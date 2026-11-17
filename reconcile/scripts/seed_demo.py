"""演示数据：三个网点分别呈现 唯一解 / 多解 / 矛盾 三种形态。

用法：PYTHONPATH=. python3 -m scripts.seed_demo
幂等：以自然键（网点+日期 / 网点+区间+总量）判断，重复执行不覆盖人工数据。
"""
from app import service
from app.storage import get_conn

SCENARIOS = [
    # 唯一解网点：三单交叠 + 一个固定日，核对即补齐
    {"branch": "演示-唯一解", "forms": [
        ("2026-11-01", "2026-11-03", 10),
        ("2026-11-02", "2026-11-04", 9),
        ("2026-11-01", "2026-11-01", 2)],
     "fixed": [("2026-11-03", 4)]},
    # 多解网点：x1+x2=6、x2+x3=5，11-02 不确定
    {"branch": "演示-多解", "forms": [
        ("2026-11-01", "2026-11-02", 6),
        ("2026-11-02", "2026-11-03", 5)],
     "fixed": []},
    # 矛盾网点：固定值已超区间总量
    {"branch": "演示-矛盾", "forms": [
        ("2026-11-01", "2026-11-02", 5)],
     "fixed_raw": [("2026-11-01", 9)]},
]


def seed() -> None:
    conn = get_conn()
    for sc in SCENARIOS:
        b = sc["branch"]
        if conn.execute("SELECT 1 FROM forms WHERE branch=?", (b,)).fetchone():
            continue
        for s, e, total in sc["forms"]:
            service.create_form({"branch": b, "start_day": s, "end_day": e, "total": total})
        for day, vol in sc.get("fixed", []):
            service.upsert_daily(b, day, vol, "reported")
        for day, vol in sc.get("fixed_raw", []):
            conn.execute(
                "INSERT INTO daily_values(branch,day,volume,source,source_form_ids) "
                "VALUES (?,?,?, 'reported', '[]')", (b, day, vol))
    print("seed done:", [s["branch"] for s in SCENARIOS])


if __name__ == "__main__":
    seed()
