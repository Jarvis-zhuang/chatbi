-- pg_readonly_setup.sql —— L3 只读保险（A 方案）+ SQLite 方言垫片（C 方案）
-- 2026-10-21 庄拍板：A + C→A。本文件可重复执行（幂等），是这两项决策的唯一落地凭证。
--
-- 为什么需要只读账号（决策记录，面试可讲）：
--   Text-to-SQL 的输入是自然语言 → LLM 是不可信组件 → 四层护栏全是应用层代码，
--   代码可能有 bug、可能被重构改漏。账号级只读是数据库强制执行的最后一堵墙，
--   不依赖调用方自觉 = 最小权限原则（least privilege）+ 纵深防御。
--   与 SQLite 的 mode=ro 语义等价，但 SQLite 那是文件级、PG 这是账号级。
--   B 方案（SET TRANSACTION READ ONLY）为何不选：是约定不是强制——
--   连接池复用忘开、autocommit、未来有人加写路径（审计/缓存表）说破就破。
--   B 保留为上 Neon/Supabase 免费档（不让建角色）时的降级路径，记入 README。
--
-- 垫片函数为什么存在（决策记录）：
--   模型生成的 SQL 天然带 SQLite 方言习惯（ROUND(x,n) / strftime）。
--   先垫片保 31 题回归零漂移（单变量原则），再在 W1 D2 做 dialect 注入根治。
--   垫片是止血不是终态，回归通过后随方言化一起退役。

\set ON_ERROR_STOP on

-- ============================================================
-- 1. 只读账号 chatbi_ro（A 方案）
-- ============================================================
-- 开发容器用固定密码；生产应走环境变量（db.py 已支持 CHATBI_PG_USER/PASSWORD 覆盖）
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'chatbi_ro') THEN
        CREATE ROLE chatbi_ro LOGIN PASSWORD 'chatbi_ro';
    END IF;
END
$$;

-- PG 默认 PUBLIC 对所有库有 CONNECT、对 public schema 有 USAGE —— GRANT 是加不是关，先收紧
REVOKE CONNECT ON DATABASE chatbi FROM PUBLIC;
REVOKE USAGE ON SCHEMA public FROM PUBLIC;

-- 再按最小权限显式授予
GRANT CONNECT ON DATABASE chatbi TO chatbi_ro;
GRANT USAGE ON SCHEMA public TO chatbi_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO chatbi_ro;

-- 未来新建的表（D3 口径表、RAG 表）自动获得只读权限，不用每次手动补
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO chatbi_ro;
-- 注意：不给 SEQUENCE 权限——只读账号永远不该自增；functions 默认 PUBLIC 可执行，够用

-- ============================================================
-- 2. SQLite 方言垫片函数（C 方案，止血用，回归后随 dialect 化退役）
-- ============================================================
-- round(double precision, integer)：PG 原生只有 round(numeric, integer)，
-- round(double, int) 不存在 → 模型生成的 ROUND(x, 4) 在 PG 直接 UndefinedFunction
CREATE OR REPLACE FUNCTION round(double precision, integer) RETURNS double precision
  AS 'SELECT round($1::numeric, $2)::double precision' LANGUAGE sql IMMUTABLE;

-- strftime(text, text)：PG 没有这个函数（SQLite 专用）。
-- 覆盖生成的 SQL 实际用到的格式符：%Y %m %d %H %M %S
-- 已知边界：SQLite 对非法日期返 NULL，这里 to_date 会抛错——生成的 SQL 只对库内
-- ISO 日期调用，实际不触发；真触发那天就是该补 dialect 注入的时候。
CREATE OR REPLACE FUNCTION strftime(text, text) RETURNS text AS $$
  SELECT to_char(to_date($2, 'YYYY-MM-DD'),
                 replace(replace(replace(replace(replace(replace(
                   $1,
                   '%Y', 'YYYY'), '%m', 'MM'), '%d', 'DD'),
                   '%H', 'HH24'), '%M', 'MI'), '%S', 'SS'))
$$ LANGUAGE sql IMMUTABLE;

-- date(text, text)：SQLite 的 date(值, 修饰符)——PG 没有。
-- 生成的 SQL 用它做「近半年/近一年」窗口：date('锚点', '-N months')（text2sql.py 558 行）。
-- ⚠️ 必须复刻 SQLite 语义而非 PG 语义：SQLite 月份加减【不做月末钳位】，日号溢出向后规范化
--   —— date('2026-08-31','-6 months')：SQLite → 2026-03-03，而 PG 的 date+interval 给 2026-02-28，
--   差 3 天 = 窗口大小不同 = 聚合结果不同 = 评测直接掉分。所以这里手工算年月日，不用 interval。
-- 已知边界：非法日期 SQLite 返 NULL、这里 to_date 抛错；'start of month' 等修饰符未实现
-- （当前生成的 SQL 不用，用到那天就是该做 dialect 注入的时候）。
CREATE OR REPLACE FUNCTION date(text, text) RETURNS text AS $$
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
$$ LANGUAGE plpgsql IMMUTABLE;

-- ============================================================
-- 3. 自证（跑完手动执行这几条验证）
-- ============================================================
-- \c chatbi chatbi_ro  (psql 8.4+ 语法；或 docker exec 里用 PGPASSWORD)
-- SELECT COUNT(*) FROM dim_plant;                    -- 应返回 60
-- INSERT INTO dim_plant(plant_name) VALUES('x');     -- 应报 permission denied
-- SELECT ROUND(1.23456, 4);                          -- 应返回 1.2346（垫片生效）
-- SELECT strftime('%Y-%m', MAX(stat_date)) FROM fact_daily_generation;  -- 应返回 2026-08
-- SELECT date('2026-08-31', '-6 months');            -- 应返回 2026-03-03（SQLite 语义，非 PG 的 02-28）
