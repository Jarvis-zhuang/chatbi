-- pg_shim.sql —— PostgreSQL 上的 SQLite 方言垫片（W1 D1 止血方案，2026-10-22 补落仓库）
--
-- 为什么要有这个文件：10/21 迁移当晚，垫片是直接 docker exec 灌进容器的，仓库里没有。
-- 容器一删（docker rm pg）垫片就消失，31 题评测集当场全崩，且没人知道为什么。
-- "环境里的状态不在版本库里 = 不可复现" —— 这是交付缺口，不是在讲战功。
--
-- 用法：docker exec -i pg psql -U postgres -d chatbi < pg_shim.sql（幂等，可重复执行）
--
-- 【垫片 vs 方言注入，是两层不是二选一】
--   垫片救的是【存量 SQL】：eval_set.json 的 25 条 gold、MOCK 规则引擎生成的 SQL，
--     全是 SQLite 方言，且它们必须在 PG 上跑（评测要跨库对比、要证明尺子没动）。
--     改这些 SQL = 改尺子，比方言错配严重得多。
--   方言注入救的是【新生成的 SQL】：LLM 路径按 prompt 走（W1 D2 才做正事）。
--   所以垫片不能删。上 Neon/Supabase 这类不让建函数的管理型数据库时垫片消失，
--   那时只读「方言注入」这条腿站着——这也是注入必须做、垫片只能当拐杖的原因。
--
-- 语义原则：复刻 SQLite 而非 PG。date('2026-08-31','-6 months') 在 SQLite 得 2026-03-03
--   （2/31 溢出到 3/3），PG 的 interval 会给 2026-02-28。垫片必须写成 SQLite 的答案，
--   否则同一份 gold 在两边判定会莫名其妙地差一天。

-- 1) round(double precision, int)：PG 的 round 只认 numeric，double 直接报
--    "function round(double precision, integer) does not exist"。
--    SQLite 的 ROUND(x, n) 对 REAL 随便用 —— 这是评测集在 PG 上第一批报错的主因。
CREATE OR REPLACE FUNCTION public.round(double precision, integer)
RETURNS double precision
LANGUAGE sql
IMMUTABLE
AS $function$SELECT round($1::numeric, $2)::double precision$function$;

-- 2) strftime(fmt, text)：只覆盖本项目实际用过的 %Y %m %d %H %M %S。
--    没用到的格式符不实现 —— 垫片实现越全，越容易让人以为"方言问题已经解决了"。
CREATE OR REPLACE FUNCTION public.strftime(text, text)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $function$
  SELECT to_char(to_date($2, 'YYYY-MM-DD'),
                 replace(replace(replace(replace(replace(replace(
                   $1,
                   '%Y', 'YYYY'), '%m', 'MM'), '%d', 'DD'),
                   '%H', 'HH24'), '%M', 'MI'), '%S', 'SS'))
$function$;

-- 3) date(text, text)：SQLite 的日期偏移。月份偏移按"自然月 + 保留日"算，
--    日溢出时进到下个月（复刻 SQLite），不是 PG interval 的月末截断。
CREATE OR REPLACE FUNCTION public.date(text, text)
RETURNS text
LANGUAGE plpgsql
IMMUTABLE
AS $function$
DECLARE
  d date := to_date($1, 'YYYY-MM-DD');
  n int;
  total int;
BEGIN
  IF $2 ~ '^[+-]?[0-9]+ months$' THEN
    n := (regexp_replace($2, '[^0-9]', '', 'g'))::int;
    IF $2 LIKE '-%' THEN n := -n; END IF;
    total := extract(year FROM d)::int * 12 + (extract(month FROM d)::int - 1) + n;
    RETURN to_char(make_date(total / 12, total % 12 + 1, 1) + (extract(day FROM d)::int - 1), 'YYYY-MM-DD');
  ELSIF $2 ~ '^[+-]?[0-9]+ days$' THEN
    n := (regexp_replace($2, '[^0-9]', '', 'g'))::int;
    IF $2 LIKE '-%' THEN n := -n; END IF;
    RETURN to_char(d + n, 'YYYY-MM-DD');
  ELSIF $2 ~ '^[+-]?[0-9]+ years$' THEN
    n := (regexp_replace($2, '[^0-9]', '', 'g'))::int;
    IF $2 LIKE '-%' THEN n := -n; END IF;
    total := extract(year FROM d)::int + n;
    RETURN to_char(make_date(total, extract(month FROM d)::int, 1) + (extract(day FROM d)::int - 1), 'YYYY-MM-DD');
  ELSE
    RETURN to_char(d, 'YYYY-MM-DD');
  END IF;
END
$function$;

-- 验证（执行后应看到三个函数）
-- SELECT proname, pg_get_function_identity_arguments(oid) FROM pg_proc
--  WHERE proname IN ('round','strftime','date') AND pronamespace = 'public'::regnamespace;
