"""端到端规则测试：唯一解 / 多解 / 矛盾 / 补录与更正争抢 / 预测冻结 / 并发。

直接运行：python -m tests.test_rules   （或 python -m unittest）
"""
import os
import tempfile
import threading
import unittest

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["RECONCILE_DB"] = _tmp.name

from app import service  # noqa: E402
from app.service import BizError  # noqa: E402
from app.storage import get_conn  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        conn = get_conn()
        for t in ("forms", "daily_values", "forecasts", "audit_log"):
            conn.execute(f"DELETE FROM {t}")

    def form(self, branch, s, e, total):
        return service.create_form({"branch": branch, "start_day": s, "end_day": e, "total": total})

    def fixed(self, branch, day, vol, source="reported"):
        return service.upsert_daily(branch, day, vol, source)

    def raw_fixed(self, branch, day, vol, source="reported"):
        """直接写库，绕过约束校验（用于构造矛盾初始状态）。"""
        conn = get_conn()
        conn.execute(
            "INSERT INTO daily_values(branch,day,volume,source,source_form_ids) VALUES (?,?,?,?,'[]')",
            (branch, day, vol, source))


class TestUniqueFill(Base):
    def test_overlapping_unique_solution_and_sources(self):
        # 单据A: d1..d3 总量 10；单据B: d2..d4 总量 9；d3 已知 4
        # x1+x2+4=10 -> x1+x2=6；x2+4+x4=9 -> x2+x4=5；多解？
        # 再加单据C: d1..d1 总量 2（单点），则 x1=2, x2=4, x4=1 唯一
        b = "川北"
        a = self.form(b, "2026-11-01", "2026-11-03", 10)
        bb = self.form(b, "2026-11-02", "2026-11-04", 9)
        c = self.form(b, "2026-11-01", "2026-11-01", 2)
        self.fixed(b, "2026-11-03", 4)
        r = service.check_branch(b)
        self.assertEqual(r["status"], "unique")
        self.assertEqual(r["assignment"], {
            "2026-11-01": 2, "2026-11-02": 4, "2026-11-04": 1})

        out = service.verify_form(a["id"])
        self.assertEqual({x["day"]: x["volume"] for x in out["filled"]},
                         {"2026-11-01": 2, "2026-11-02": 4, "2026-11-04": 1})
        # 待核对单据同时生效
        for f in service.list_forms(b):
            self.assertEqual(f["status"], "effective")
        # 来源保留：只记录实际覆盖该日的单据
        rows = {d["day"]: d for d in service.list_daily(b)}
        self.assertEqual(rows["2026-11-02"]["source"], "derived")
        self.assertEqual(set(rows["2026-11-02"]["source_form_ids"]), {a["id"], bb["id"]})
        self.assertEqual(rows["2026-11-01"]["source_form_ids"], [a["id"], c["id"]])
        self.assertEqual(rows["2026-11-04"]["source_form_ids"], [bb["id"]])
        # 已有日量不改
        self.assertEqual(rows["2026-11-03"]["source"], "reported")
        self.assertEqual(rows["2026-11-03"]["volume"], 4)

    def test_verify_retry_is_idempotent(self):
        b = "东河"
        a = self.form(b, "2026-12-01", "2026-12-02", 7)
        self.fixed(b, "2026-12-01", 3)
        service.verify_form(a["id"])
        before = service.list_daily(b)
        again = service.verify_form(a["id"])
        self.assertEqual(again["filled"], [])
        self.assertEqual(service.list_daily(b), before)
        # 版本只在真正核对时增加一次
        self.assertEqual(service.list_forms(b)[0]["version"], 2)

    def test_negative_and_bad_inputs_rejected(self):
        with self.assertRaises(BizError) as cm:
            service.create_form({"branch": "x", "start_day": "2026-11-02",
                                 "end_day": "2026-11-01", "total": 1})
        self.assertEqual(cm.exception.code, "bad_range")
        with self.assertRaises(BizError):
            service.upsert_daily("x", "2026-11-01", -1, "reported")
        with self.assertRaises(BizError):
            service.upsert_daily("x", "not-a-date", 1, "reported")


class TestMultiple(Base):
    def test_multiple_lists_uncertain_days_and_no_fill(self):
        # x1+x2=6 且 x2+x3=5：自由变量 x2 -> 多解
        b = "川南"
        a = self.form(b, "2026-11-01", "2026-11-02", 6)
        bb = self.form(b, "2026-11-02", "2026-11-03", 5)
        r = service.check_branch(b)
        self.assertEqual(r["status"], "multiple")
        self.assertEqual(set(r["uncertain"]),
                         {"2026-11-01", "2026-11-02", "2026-11-03"})
        with self.assertRaises(BizError) as cm:
            service.verify_form(a["id"])
        self.assertEqual(cm.exception.code, "multiple")
        # 未补任何数据
        self.assertEqual(service.list_daily(b), [])
        # 补一个固定值后变唯一：x1=6 -> x2=0, x3=5
        self.fixed(b, "2026-11-01", 6)
        r2 = service.check_branch(b)
        self.assertEqual(r2["status"], "unique")
        self.assertEqual(r2["assignment"], {"2026-11-02": 0, "2026-11-03": 5})
        service.verify_form(a["id"])
        rows = {d["day"]: d["volume"] for d in service.list_daily(b)}
        self.assertEqual(rows, {"2026-11-01": 6, "2026-11-02": 0, "2026-11-03": 5})


class TestConflict(Base):
    def test_direct_conflict_fixed_exceeds_total(self):
        b = "西浦"
        a = self.form(b, "2026-11-01", "2026-11-02", 5)
        self.raw_fixed(b, "2026-11-01", 9)  # 固定 9 已超总量 5，绕过校验构造矛盾
        r = service.check_branch(b)
        self.assertEqual(r["status"], "infeasible")
        self.assertEqual(r["direct_conflicts"], [a["id"]])
        with self.assertRaises(BizError) as cm:
            service.verify_form(a["id"])
        self.assertEqual(cm.exception.status, 409)
        self.assertEqual(cm.exception.extra["direct_conflicts"], [a["id"]])
        # 拒绝事件即使事务回滚也必须留痕
        acts = [x["action"] for x in service.list_audit()]
        self.assertIn("verify_rejected", acts)

    def test_equation_conflict_group(self):
        # A: x1+x2=3；B: x2+x3=3；C: x1+x3=9 -> 奇数和矛盾（三式相加 2(x1+x2+x3)=15）
        b = "新城"
        a = self.form(b, "2026-11-01", "2026-11-02", 3)
        bb = self.form(b, "2026-11-02", "2026-11-03", 3)
        c = self.form(b, "2026-11-01", "2026-11-01", 0)  # 让 x1=0 后与 x1+x3=9、x2+x3=3 冲突
        # x1=0 => x2=3, x3 需同时=0(x2+x3=3)和=9(x1+x3=9)，矛盾；C 与 B 冲突组
        d = self.form(b, "2026-11-01", "2026-11-03", 9)
        r = service.check_branch(b)
        self.assertEqual(r["status"], "infeasible")
        self.assertIn(d["id"], r["conflicts"])
        self.assertIn(bb["id"], r["conflicts"])
        # 删除过滤后剩余组本身不可行、且是极小的
        ids = set(r["conflicts"])
        alive = [f for f in service.list_forms(b) if f["id"] in ids]
        from app.solver import _is_feasible
        self.assertFalse(_is_feasible(alive, {}))

    def test_manual_revision_conflict_keeps_old_data(self):
        b = "临港"
        a = self.form(b, "2026-11-01", "2026-11-02", 4)
        self.fixed(b, "2026-11-01", 1)
        service.verify_form(a["id"])  # x2=3 补齐
        with self.assertRaises(BizError) as cm:
            service.upsert_daily(b, "2026-11-01", 10, "manual")
        self.assertEqual(cm.exception.code, "infeasible")
        rows = {d["day"]: d for d in service.list_daily(b)}
        self.assertEqual(rows["2026-11-01"]["volume"], 1)          # 原值保留
        self.assertEqual(rows["2026-11-01"]["source"], "reported")
        self.assertEqual(rows["2026-11-02"]["volume"], 3)          # 原补齐保留

    def test_correct_effective_form_failure_keeps_everything(self):
        b = "山前"
        a = self.form(b, "2026-11-01", "2026-11-02", 5)
        self.fixed(b, "2026-11-01", 2)
        service.verify_form(a["id"])
        old_forms = service.list_forms(b)
        old_daily = service.list_daily(b)
        with self.assertRaises(BizError):
            service.correct_form(a["id"], {"branch": b,
                "start_day": "2026-11-01", "end_day": "2026-11-02", "total": 1})
        self.assertEqual(service.list_forms(b), old_forms)
        self.assertEqual(service.list_daily(b), old_daily)


class TestCorrectionRecalc(Base):
    def test_correction_recomputes_derived_values(self):
        b = "湖滨"
        a = self.form(b, "2026-11-01", "2026-11-03", 9)
        self.fixed(b, "2026-11-01", 2)
        self.fixed(b, "2026-11-02", 3)
        service.verify_form(a["id"])
        self.assertEqual([d for d in service.list_daily(b) if d["day"] == "2026-11-03"][0]["volume"], 4)
        # 更正总量 9 -> 12，缺日重算为 7
        out = service.correct_form(a["id"], {"branch": b,
            "start_day": "2026-11-01", "end_day": "2026-11-03", "total": 12})
        self.assertEqual(out["form"]["version"], 3)  # 核对+1、更正+1
        rows = {d["day"]: d for d in service.list_daily(b)}
        self.assertEqual(rows["2026-11-03"]["volume"], 7)
        self.assertEqual(rows["2026-11-03"]["source"], "derived")
        # 固定值不变
        self.assertEqual(rows["2026-11-01"]["volume"], 2)
        self.assertEqual(rows["2026-11-02"]["volume"], 3)

    def test_correction_shrinking_range_drops_orphan_derived(self):
        b = "湖滨2"
        a = self.form(b, "2026-11-01", "2026-11-03", 6)
        self.fixed(b, "2026-11-01", 1)
        self.fixed(b, "2026-11-02", 2)
        service.verify_form(a["id"])  # x3=3 唯一补齐
        # 区间缩短到 d1..d2：d3 的 derived 失去覆盖，重算后应删除
        service.correct_form(a["id"], {"branch": b,
            "start_day": "2026-11-01", "end_day": "2026-11-02", "total": 3})
        days = {d["day"] for d in service.list_daily(b)}
        self.assertNotIn("2026-11-03", days)


class TestForecast(Base):
    def test_pending_blocks_forecast_and_effective_accepts(self):
        b = "川北"
        a = self.form(b, "2026-11-01", "2026-11-02", 5)
        self.fixed(b, "2026-11-01", 5)
        with self.assertRaises(BizError) as cm:
            service.accept_forecast(b, "2026-11-02")
        self.assertEqual(cm.exception.code, "pending_blocks_forecast")
        self.fixed(b, "2026-11-02", 0)  # 固定后 a 方程唯一（5+0=5）
        service.verify_form(a["id"])
        # 核对后无待核对单据，可受理 11-10 预测
        out = service.accept_forecast(b, "2026-11-10")
        self.assertTrue(out["created"])
        self.assertEqual(out["forecast"]["value"], 3)  # 固定均值 (5+0)/2 四舍五入
        self.assertFalse(out["forecast"]["retained"])

        # 新增一张覆盖未来目标日的待核对单据 -> 阻止受理（未核对部分阻止预测）
        p = self.form(b, "2026-12-20", "2026-12-21", 3)
        with self.assertRaises(BizError) as cm2:
            service.accept_forecast(b, "2026-12-21")
        self.assertEqual(cm2.exception.code, "pending_blocks_forecast")
        self.assertEqual(cm2.exception.extra["forms"], [p["id"]])
        # 不覆盖目标日时不阻止
        again = service.accept_forecast(b, "2026-11-10")
        self.assertFalse(again["created"])

    def test_old_forecast_retained_on_reaccept(self):
        b = "川南"
        a = self.form(b, "2026-11-01", "2026-11-01", 4)
        self.fixed(b, "2026-11-01", 4)
        service.verify_form(a["id"])
        first = service.accept_forecast(b, "2026-11-08")["forecast"]
        # 新增固定值改变依据
        self.fixed(b, "2026-11-02", 10)
        again = service.accept_forecast(b, "2026-11-08")
        self.assertFalse(again["created"])
        self.assertEqual(again["forecast"]["value"], first["value"])  # 旧值保留
        self.assertTrue(again["forecast"]["retained"])
        # 依据未变时重复受理不标记 retained
        service.accept_forecast(b, "2026-11-08")
        self.assertTrue(service.list_forecasts(b)[0]["retained"])  # 标记保持
        # 只有一条预测记录
        self.assertEqual(len(service.list_forecasts(b)), 1)


class TestConcurrency(Base):
    def test_parallel_correction_and_verify_contention(self):
        """补录(核对)与更正争抢：串行化后必有一方看到一致状态，数据不损坏。"""
        b = "西浦"
        a = self.form(b, "2026-11-01", "2026-11-02", 5)
        self.fixed(b, "2026-11-01", 2)
        errors, done = [], []

        def verify():
            try:
                done.append(("verify", service.verify_form(a["id"])))
            except BizError as e:
                errors.append(("verify", e.code))

        def correct():
            try:
                done.append(("correct", service.correct_form(a["id"], {"branch": b,
                    "start_day": "2026-11-01", "end_day": "2026-11-02", "total": 8})))
            except BizError as e:
                errors.append(("correct", e.code))

        t1, t2 = threading.Thread(target=verify), threading.Thread(target=correct)
        t1.start(); t2.start(); t1.join(); t2.join()

        # 最终状态必须自洽：所有生效单据方程成立，derived 值与当前唯一解一致
        final = service.check_branch(b)
        self.assertEqual(final["status"], "unique")
        rows = {d["day"]: d for d in service.list_daily(b)}
        total_now = service.list_forms(b)[0]["total"]
        self.assertEqual(sum(r["volume"] for r in rows.values()), total_now)
        # 至少一个操作成功；另一个若失败必须是业务拒绝而非数据损坏
        self.assertTrue(done)

    def test_parallel_forecast_creates_single_row(self):
        b = "新城"
        a = self.form(b, "2026-11-01", "2026-11-01", 3)
        self.fixed(b, "2026-11-01", 3)
        service.verify_form(a["id"])
        results = []

        def fire():
            results.append(service.accept_forecast(b, "2026-11-09"))

        threads = [threading.Thread(target=fire) for _ in range(6)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(len(service.list_forecasts(b)), 1)
        self.assertEqual(sum(1 for r in results if r["created"]), 1)
        self.assertEqual(sum(1 for r in results if not r["created"]), 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
