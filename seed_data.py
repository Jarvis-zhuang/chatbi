# -*- coding: utf-8 -*-
"""
seed_data.py —— 生成 ChatBI 的合成业务库（SQLite）

数据全部本地合成，不爬不买。数据库落在 data/chatbi.db，第二天（D2）换成 Postgres 时
只要保持表名和字段名一致，text2sql.py 不用改。

 why synthetic？
 - 面试时大方承认数据是合成的，但分布必须合理：西北集中式大基地 100-500MW、
   等效利用小时 1400-1800h；中部/沿海分布式 5-50MW、900-1400h。懂业务的人一眼能看出来。
"""
import os
import random
import sqlite3
from datetime import date, timedelta

random.seed(20260928)  # 固定随机种子：评测结果可复现，这是做 eval 的前提

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "chatbi.db")

# ---------------------------------------------------------------- 区域画像
# (省份, 区域, 类型偏好, 装机区间MW, 年均等效利用小时区间, 弃光率区间)
PROVINCE_PROFILE = [
    ("青海", "西北", "集中式", (200, 500), (1500, 1800), (0.03, 0.09)),
    ("新疆", "西北", "集中式", (150, 500), (1450, 1750), (0.03, 0.10)),
    ("甘肃", "西北", "集中式", (120, 400), (1400, 1700), (0.02, 0.08)),
    ("宁夏", "西北", "集中式", (100, 300), (1400, 1650), (0.02, 0.07)),
    ("内蒙古", "华北", "集中式", (100, 400), (1400, 1700), (0.02, 0.06)),
    ("山西", "华北", "集中式", (50, 200), (1250, 1450), (0.01, 0.04)),
    ("河北", "华北", "分布式", (20, 80), (1150, 1350), (0.01, 0.03)),
    ("山东", "华东", "分布式", (5, 50), (1100, 1350), (0.00, 0.02)),
    ("河南", "华中", "分布式", (5, 40), (1100, 1300), (0.00, 0.02)),
    ("江苏", "华东", "分布式", (5, 50), (1000, 1200), (0.00, 0.02)),
    ("浙江", "华东", "分布式", (5, 40), (950, 1150), (0.00, 0.02)),
    ("安徽", "华中", "分布式", (5, 45), (1050, 1250), (0.00, 0.02)),
    ("广东", "南方", "分布式", (5, 40), (900, 1100), (0.00, 0.02)),
    ("福建", "南方", "分布式", (5, 35), (900, 1100), (0.00, 0.02)),
]

INVERTER_BRANDS = ["阳光电源", "华为", "上能电气", "特变电工", "固德威", "锦浪科技"]
DEVICE_TYPES = ["逆变器", "汇流箱", "变压器", "支架跟踪系统", "气象仪"]
FAULT_TYPES = ["逆变器过温", "通信中断", "直流侧绝缘阻抗低", "跟踪支架卡涩", "电网电压越限"]

PLANT_SUFFIX = ["光伏电站", "光伏发电厂", "农光互补电站", "山地光伏电站", "渔光互补电站"]
PLANT_PREFIX = ["金辰", "腾晖", "恒源", "星耀", "瑞泰", "东旭", "天合", "协鑫", "中环", "蓝星",
                "广宇", "瀚海", "戈尔", "青格尔", "沙丘", "玉门", "贺兰", "河套", "胶州", "淮上"]

START_DATE = date(2024, 9, 1)
DAYS = 730  # 两年日发电明细


def season_factor(d):
    """季节因子：夏季辐照高、冬季低，北半球光伏的典型形态"""
    m = d.month
    table = {1: 0.72, 2: 0.82, 3: 1.02, 4: 1.15, 5: 1.22, 6: 1.18,
             7: 1.12, 8: 1.10, 9: 1.05, 10: 0.95, 11: 0.80, 12: 0.70}
    return table[m]


def build_schema(conn):
    cur = conn.cursor()
    cur.executescript(
        """
        DROP TABLE IF EXISTS fact_daily_generation;
        DROP TABLE IF EXISTS fact_device_fault;
        DROP TABLE IF EXISTS dim_device;
        DROP TABLE IF EXISTS dim_plant;
        DROP TABLE IF EXISTS metric_glossary;

        CREATE TABLE dim_plant (
            plant_id          INTEGER PRIMARY KEY,
            plant_name        TEXT    NOT NULL,        -- 电站名称
            province          TEXT    NOT NULL,        -- 省份
            region            TEXT    NOT NULL,        -- 区域（西北/华北/华东/华中/南方）
            station_type      TEXT    NOT NULL,        -- 集中式 / 分布式
            capacity_mw       REAL    NOT NULL,        -- 装机容量（MW）
            inverter_brand    TEXT    NOT NULL,        -- 逆变器品牌
            inverter_efficiency REAL  NOT NULL,        -- 逆变器加权转换效率
            grid_connect_date TEXT    NOT NULL         -- 并网日期
        );

        CREATE TABLE fact_daily_generation (
            id                INTEGER PRIMARY KEY,
            plant_id          INTEGER NOT NULL,        -- 电站
            stat_date         TEXT    NOT NULL,        -- 统计日期 YYYY-MM-DD
            generation_kwh    REAL    NOT NULL,        -- 日发电量（kWh）
            irradiation       REAL    NOT NULL,        -- 日辐照量（kWh/m2）
            equivalent_hours  REAL    NOT NULL,        -- 当日等效利用小时（h）
            curtailment_kwh   REAL    NOT NULL,        -- 当日弃光电量（kWh）
            curtailment_rate  REAL    NOT NULL         -- 当日弃光率
        );

        CREATE TABLE dim_device (
            device_id         INTEGER PRIMARY KEY,
            plant_id          INTEGER NOT NULL,        -- 所属电站
            device_type       TEXT    NOT NULL,        -- 设备类型
            device_model      TEXT    NOT NULL         -- 设备型号
        );

        CREATE TABLE fact_device_fault (
            id                INTEGER PRIMARY KEY,
            device_id         INTEGER NOT NULL,        -- 设备
            plant_id          INTEGER NOT NULL,        -- 电站
            fault_date        TEXT    NOT NULL,        -- 故障日期
            fault_type        TEXT    NOT NULL,        -- 故障类型
            downtime_minutes  INTEGER NOT NULL         -- 停机时长（分钟）
        );

        CREATE TABLE metric_glossary (
            metric_id         INTEGER PRIMARY KEY,
            metric_name       TEXT    NOT NULL,        -- 指标名称
            aliases           TEXT,                    -- 别名/业务黑话，逗号分隔
            definition        TEXT    NOT NULL,        -- 口径定义
            formula           TEXT    NOT NULL,        -- 计算公式
            unit              TEXT,                    -- 单位
            source_doc        TEXT                     -- 口径来源文档（溯源用）
        );
        """
    )
    conn.commit()


GLOSRARY = [
    ("装机容量", "容量,装机,规模",
     "电站交流侧额定容量之和，代表这座电站在标准测试条件下的最大出力能力",
     "SUM(capacity_mw)", "MW", "公司指标口径表 V3.2"),
    ("发电量", "电量,产出,发了多少度电",
     "统计周期内逆变器出口侧上网电量之和",
     "SUM(generation_kwh)", "kWh", "公司指标口径表 V3.2"),
    ("等效利用小时", "利用小时,等效小时,发电小时数",
     "统计周期内发电量折算到额定容量下的满发小时数，用于横向对比不同规模电站的效率",
     "SUM(generation_kwh) / 电站额定容量(capacity_mw) / 1000"
     "（分母取电站额定容量，禁止对 JOIN 后的明细行重复求和）", "h", "公司指标口径表 V3.2"),
    ("弃光率", "限发率,弃风弃光,丢弃率",
     "因电网消纳受限等原因被迫放弃的电量占理论发电量的比例",
     "SUM(curtailment_kwh) / (SUM(generation_kwh) + SUM(curtailment_kwh))", "%", "调度运行报表口径 V1.5"),
    ("辐照量", "辐照度,光照,太阳辐射",
     "单位面积接收的太阳辐射能量，是发电量的强相关驱动因子",
     "AVG(irradiation)", "kWh/m2", "气象数据口径说明"),
    ("逆变器转换效率", "逆变器效率,转换效率,逆变效率",
     "逆变器输出交流功率与输入直流功率之比，加权平均口径",
     "SUM(发电量) / SUM(直流侧输入电量)，本库取 dim_plant.inverter_efficiency 加权值", "%", "设备性能口径 V2.0"),
    ("集中式电站", "集中式,大基地,地面电站",
     "大规模集中建设、直接并入高压电网的光伏电站，单体规模通常在 50MW 以上",
     "station_type = '集中式'", "-", "资产分类标准"),
    ("分布式电站", "分布式,屋顶光伏,户用",
     "靠近用户侧建设、以就地消纳为主的光伏电站，单体规模通常低于 50MW",
     "station_type = '分布式'", "-", "资产分类标准"),
    ("同比", "去年同期,YoY,比去年",
     "与上一年的同一统计周期相比的变化幅度",
     "(本期值 - 去年同期值) / 去年同期值", "%", "通用统计口径"),
    ("环比", "上月环比,MoM,较上月",
     "与上一个相邻统计周期相比的变化幅度",
     "(本期值 - 上期值) / 上期值", "%", "通用统计口径"),
    ("月度发电量", "月发电量,每月电量",
     "按自然月汇总的发电量",
     "SUM(generation_kwh) GROUP BY strftime('%Y-%m', stat_date)", "kWh", "公司指标口径表 V3.2"),
    ("前三季度发电量", "1-9月电量,前三季度",
     "自然年内 1 月至 9 月的发电量累计值",
     "SUM(generation_kwh) WHERE 月份 BETWEEN 1 AND 9", "kWh", "通用统计口径"),
    ("装机占比", "占比,构成,份额",
     "某类资产装机容量占总装机容量的比例",
     "SUM(capacity_mw) 分组 / SUM(capacity_mw) 全量", "%", "资产分析口径"),
    ("单位装机发电量", "单位发电,每兆瓦发电",
     "每兆瓦装机对应的发电量，剔除规模影响后的产出指标",
     "SUM(generation_kwh) / SUM(capacity_mw)", "kWh/MW", "公司指标口径表 V3.2"),
    ("利用小时达成率", "达成率,完成率",
     "实际等效利用小时与设计值之比",
     "实际等效利用小时 / 设计等效利用小时", "%", "运行考核口径"),
    ("设备故障次数", "故障次数,缺陷次数,报警次数",
     "统计周期内设备发生的故障告警记录条数",
     "COUNT(*) FROM fact_device_fault", "次", "运维报表口径 V1.2"),
    ("平均停机时长", "MTTR,修复时长,停机时长",
     "单次故障从发生到恢复的平均时长",
     "AVG(downtime_minutes)", "分钟", "运维报表口径 V1.2"),
    ("可用率", "PR,性能比,电站效率",
     "实际发电量与理论发电量之比，综合反映设备与运维水平",
     "实际发电量 / (装机容量 * 峰值日照时数)", "%", "运维报表口径 V1.2"),
    ("平均无故障时间", "MTBF,故障间隔",
     "两次相邻故障之间的平均运行时长",
     "总运行时长 / 故障次数", "小时", "运维报表口径 V1.2"),
    ("弃光电量", "限发电量,损失电量",
     "因电网限发而未发出的电量绝对值",
     "SUM(curtailment_kwh)", "kWh", "调度运行报表口径 V1.5"),
    ("峰值日照时数", "峰值小时,标准日照",
     "折算到 1000W/m2 标准条件下的日照小时数",
     "SUM(irradiation)", "h", "气象数据口径说明"),
    ("西北大基地", "大基地,沙戈荒",
     "西北区域集中式大型风光基地项目的统称，本库指省域为西北且 station_type='集中式' 的电站",
     "region='西北' AND station_type='集中式'", "-", "资产分类标准"),
    ("容配比", "交直流配比,DC/AC",
     "直流侧组件容量与交流侧逆变器容量之比",
     "直流侧容量 / 交流侧容量", "-", "设计规范"),
    ("农光互补", "农光,板上发电板下种植",
     "光伏阵列与农业种植结合的土地复合利用模式",
     "plant_name LIKE '%农光%'", "-", "资产分类标准"),
    ("渔光互补", "渔光,水上光伏",
     "光伏阵列与水产养殖结合的水面复合利用模式",
     "plant_name LIKE '%渔光%'", "-", "资产分类标准"),
    ("账面净资产收益率", "ROE,净资产收益",
     "净利润与平均净资产之比（财务指标，本库不涉及，用于评测拒答负样本）",
     "净利润 / 平均净资产", "%", "财务口径"),
]


def load_glossary(conn):
    rows = []
    for i, (name, alias, definition, formula, unit, src) in enumerate(GLOSRARY, start=1):
        rows.append((i, name, alias, definition, formula, unit, src))
    conn.executemany(
        "INSERT INTO metric_glossary (metric_id, metric_name, aliases, definition, formula, unit, source_doc)"
        " VALUES (?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()


def gen_plants(conn, plant_count=60):
    plants = []
    used_names = set()
    for pid in range(1, plant_count + 1):
        province, region, prefer_type, cap_rng, hours_rng, curt_rng = PROVINCE_PROFILE[
            (pid - 1) % len(PROVINCE_PROFILE)
        ]
        station_type = prefer_type if random.random() < 0.85 else (
            "分布式" if prefer_type == "集中式" else "集中式"
        )
        name = f"{random.choice(PLANT_PREFIX)}{random.choice(PLANT_SUFFIX)}"
        while name in used_names:
            name = f"{random.choice(PLANT_PREFIX)}{random.choice(PLANT_SUFFIX)}"
        used_names.add(name)

        if station_type == "集中式":
            capacity = round(random.uniform(max(cap_rng[0], 60), cap_rng[1]), 1)
        else:
            capacity = round(random.uniform(5, 50), 1)

        plants.append(
            {
                "plant_id": pid,
                "plant_name": f"{name}-{pid:02d}",
                "province": province,
                "region": region,
                "station_type": station_type,
                "capacity_mw": capacity,
                "inverter_brand": random.choice(INVERTER_BRANDS),
                # 逆变器加权转换效率：主流机型 96%-99%
                "inverter_efficiency": round(random.uniform(0.955, 0.991), 4),
                "grid_connect_date": f"20{random.randint(19, 24)}-{random.randint(1, 12):02d}-{random.randint(1, 28):02d}",
                "_annual_hours": random.uniform(hours_rng[0], hours_rng[1]),
                "_curtail": random.uniform(curt_rng[0], curt_rng[1]),
            }
        )

    conn.executemany(
        "INSERT INTO dim_plant VALUES (:plant_id,:plant_name,:province,:region,:station_type,"
        ":capacity_mw,:inverter_brand,:inverter_efficiency,:grid_connect_date)",
        [{k: v for k, v in p.items() if not k.startswith("_")} for p in plants],
    )
    conn.commit()
    return plants


def gen_daily(conn, plants):
    rows = []
    row_id = 0
    for p in plants:
        cap_kw = p["capacity_mw"] * 1000
        base_daily_hours = p["_annual_hours"] / 365.0
        for d in range(DAYS):
            day = START_DATE + timedelta(days=d)
            sf = season_factor(day)
            noise = random.gauss(1.0, 0.14)          # 天气噪声
            if noise < 0.55:
                noise = 0.55
            day_hours = base_daily_hours * sf * noise
            generation = cap_kw * day_hours
            irradiation = round(day_hours * random.uniform(0.95, 1.05), 3)
            curt_rate = min(0.35, max(0.0, p["_curtail"] * random.uniform(0.2, 1.8)))
            # 弃光电量 = 理论发电量 * 弃光率
            curt_kwh = round(generation * curt_rate, 2)
            row_id += 1
            rows.append(
                (
                    row_id,
                    p["plant_id"],
                    day.isoformat(),
                    round(generation, 2),
                    irradiation,
                    round(day_hours, 3),
                    curt_kwh,
                    round(curt_rate, 4),
                )
            )
        if len(rows) >= 20000:
            conn.executemany("INSERT INTO fact_daily_generation VALUES (?,?,?,?,?,?,?,?)", rows)
            conn.commit()
            rows = []
    if rows:
        conn.executemany("INSERT INTO fact_daily_generation VALUES (?,?,?,?,?,?,?,?)", rows)
        conn.commit()


def gen_devices_and_faults(conn, plants):
    devices = []
    device_id = 0
    for p in plants:
        for _ in range(random.randint(6, 12)):
            device_id += 1
            devices.append(
                (
                    device_id,
                    p["plant_id"],
                    random.choice(DEVICE_TYPES),
                    f"{random.choice(['SG', 'HW', 'SN', 'Growatt'])}-{random.randint(100, 999)}",
                )
            )
    conn.executemany("INSERT INTO dim_device VALUES (?,?,?,?)", devices)
    conn.commit()

    faults = []
    fid = 0
    for dev_id, plant_id, dev_type, _ in devices:
        for _ in range(random.randint(0, 5)):
            fid += 1
            day = START_DATE + timedelta(days=random.randint(0, DAYS - 1))
            faults.append(
                (
                    fid,
                    dev_id,
                    plant_id,
                    day.isoformat(),
                    random.choice(FAULT_TYPES),
                    random.randint(10, 900),
                )
            )
    conn.executemany("INSERT INTO fact_device_fault VALUES (?,?,?,?,?,?)", faults)
    conn.commit()


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    conn = sqlite3.connect(DB_PATH)
    build_schema(conn)
    plants = gen_plants(conn)
    gen_daily(conn, plants)
    gen_devices_and_faults(conn, plants)
    load_glossary(conn)

    cur = conn.cursor()
    stats = {
        "plants": cur.execute("SELECT COUNT(*) FROM dim_plant").fetchone()[0],
        "daily": cur.execute("SELECT COUNT(*) FROM fact_daily_generation").fetchone()[0],
        "devices": cur.execute("SELECT COUNT(*) FROM dim_device").fetchone()[0],
        "faults": cur.execute("SELECT COUNT(*) FROM fact_device_fault").fetchone()[0],
        "glossary": cur.execute("SELECT COUNT(*) FROM metric_glossary").fetchone()[0],
        "max_date": cur.execute("SELECT MAX(stat_date) FROM fact_daily_generation").fetchone()[0],
    }
    conn.close()
    print("[seed] db =", DB_PATH)
    for k, v in stats.items():
        print(f"[seed] {k:<10} = {v}")


if __name__ == "__main__":
    main()
