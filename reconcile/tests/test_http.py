"""HTTP 端到端测试：真实起服务，走 JSON API 验证全流程与并发。"""
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["RECONCILE_DB"] = _tmp.name

from app.http_server import Handler  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402

_server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
PORT = _server.server_address[1]
threading.Thread(target=_server.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{PORT}"


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def q(path, **params):
    return path + "?" + urllib.parse.urlencode(params)


def check_path(branch):
    return "/api/branches/" + urllib.parse.quote(branch) + "/check"


class HttpFlowTest(unittest.TestCase):
    def setUp(self):
        # 清空库
        from app.storage import get_conn
        conn = get_conn()
        for t in ("forms", "daily_values", "forecasts", "audit_log"):
            conn.execute(f"DELETE FROM {t}")

    def test_full_flow_unique_multiple_conflict_and_forecast(self):
        b = "API网点"
        # 健康检查与静态页
        with urllib.request.urlopen(BASE + "/health") as r:
            self.assertEqual(json.loads(r.read())["status"], "ok")
        with urllib.request.urlopen(BASE + "/") as r:
            self.assertIn("区间汇总", r.read().decode())

        # 登记两张交叠单据
        s, f1 = call("POST", "/api/forms", {"branch": b,
            "start_day": "2026-11-01", "end_day": "2026-11-03", "total": 10})
        self.assertEqual(s, 201)
        _, f2 = call("POST", "/api/forms", {"branch": b,
            "start_day": "2026-11-02", "end_day": "2026-11-04", "total": 9})
        _, f3 = call("POST", "/api/forms", {"branch": b,
            "start_day": "2026-11-01", "end_day": "2026-11-01", "total": 2})

        # 试算：固定 11-03=4 前先看多解
        _, chk0 = call("GET", check_path(b))
        self.assertEqual(chk0["status"], "multiple")
        self.assertTrue(chk0["uncertain"])

        # 录入固定日量
        s, _ = call("POST", "/api/daily", {"branch": b, "day": "2026-11-03",
                                           "volume": 4, "source": "reported"})
        self.assertEqual(s, 200)
        _, chk = call("GET", check_path(b))
        self.assertEqual(chk["status"], "unique")

        # 核对生效（试一次重复请求幂等）
        s, out = call("POST", f"/api/forms/{f1['id']}/verify", {})
        self.assertEqual(s, 200)
        self.assertEqual(len(out["filled"]), 3)
        s, retry = call("POST", f"/api/forms/{f1['id']}/verify", {})
        self.assertEqual(s, 200)
        self.assertEqual(retry["filled"], [])

        # 日量来源可查
        _, daily = call("GET", q("/api/daily", branch=b))
        sources = {d["day"]: d for d in daily}
        self.assertEqual(sources["2026-11-02"]["source"], "derived")
        self.assertEqual(sources["2026-11-02"]["source_form_ids"], [f1["id"], f2["id"]])
        self.assertEqual(sources["2026-11-03"]["source"], "reported")

        # 矛盾修订被拒、原数据保留
        s, err = call("POST", "/api/daily", {"branch": b, "day": "2026-11-03",
                                             "volume": 999, "source": "manual"})
        self.assertEqual(s, 409)
        self.assertEqual(err["code"], "infeasible")
        self.assertIn("conflicts", err)
        _, daily2 = call("GET", q("/api/daily", branch=b))
        self.assertEqual({d["day"]: d for d in daily2}["2026-11-03"]["volume"], 4)

        # 生效单据更正 -> 交叠单据需整套一致：只改 f1 会与 f2 冲突，失败并保留
        s, bad = call("PUT", f"/api/forms/{f1['id']}", {"branch": b,
            "start_day": "2026-11-01", "end_day": "2026-11-03", "total": 13})
        self.assertEqual(s, 409)
        self.assertEqual(bad["code"], "infeasible")
        # 先改 f2=12，再改 f1=13：d2=7、d4=1，整套重算成功
        s, _ = call("PUT", f"/api/forms/{f2['id']}", {"branch": b,
            "start_day": "2026-11-02", "end_day": "2026-11-04", "total": 12})
        self.assertEqual(s, 200)
        s, _ = call("PUT", f"/api/forms/{f1['id']}", {"branch": b,
            "start_day": "2026-11-01", "end_day": "2026-11-03", "total": 13})
        self.assertEqual(s, 200)
        _, daily3 = call("GET", q("/api/daily", branch=b))
        d3 = {d["day"]: d for d in daily3}
        self.assertEqual(d3["2026-11-03"]["volume"], 4)       # 固定不动
        self.assertEqual(d3["2026-11-02"]["volume"], 7)       # 13-2-4
        self.assertEqual(d3["2026-11-04"]["volume"], 1)       # 12-7-4

        # 预测受理：无待核对 -> 成功；再登记待核对覆盖 -> 阻止；旧值保留
        s, fc = call("POST", "/api/forecasts", {"branch": b, "target_day": "2026-11-20"})
        self.assertEqual(s, 200)
        self.assertTrue(fc["created"])
        s, again = call("POST", "/api/forecasts", {"branch": b, "target_day": "2026-11-20"})
        self.assertEqual(s, 200)
        self.assertFalse(again["created"])
        self.assertEqual(again["forecast"]["value"], fc["forecast"]["value"])

        call("POST", "/api/forms", {"branch": b,
            "start_day": "2026-11-18", "end_day": "2026-11-22", "total": 50})
        s, blocked = call("POST", "/api/forecasts", {"branch": b, "target_day": "2026-11-20"})
        self.assertEqual(s, 409)
        self.assertEqual(blocked["code"], "pending_blocks_forecast")

        # 审计有记录
        _, audits = call("GET", "/api/audit")
        actions = {a["action"] for a in audits}
        self.assertIn("verify_form", actions)
        self.assertIn("correct_form", actions)
        self.assertIn("forecast_blocked", actions)

    def test_validation_errors(self):
        s, err = call("POST", "/api/forms", {"branch": "x",
            "start_day": "2026-11-05", "end_day": "2026-11-01", "total": 1})
        self.assertEqual(s, 422)
        self.assertEqual(err["code"], "bad_range")
        s, err = call("POST", "/api/forms", {"branch": "x",
            "start_day": "2026-11-01", "end_day": "2026-11-02", "total": -3})
        self.assertEqual(s, 422)
        s, err = call("POST", "/api/forms", {"branch": "x",
            "start_day": "bad", "end_day": "2026-11-02", "total": 1})
        self.assertEqual(s, 422)
        self.assertEqual(err["code"], "bad_date")
        s, err = call("GET", "/api/branches/"+urllib.parse.quote("不存在")+"/check")
        self.assertEqual(s, 200)
        self.assertEqual(err["status"], "no_form")

    def test_concurrent_mixed_requests_keep_consistency(self):
        b = "并发网点"
        _, f1 = call("POST", "/api/forms", {"branch": b,
            "start_day": "2026-11-01", "end_day": "2026-11-02", "total": 5})
        call("POST", "/api/daily", {"branch": b, "day": "2026-11-01",
                                    "volume": 2, "source": "reported"})

        def verify():
            return call("POST", f"/api/forms/{f1['id']}/verify", {})

        def correct(total):
            return call("PUT", f"/api/forms/{f1['id']}", {"branch": b,
                "start_day": "2026-11-01", "end_day": "2026-11-02", "total": total})

        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = [ex.submit(verify), ex.submit(verify),
                    ex.submit(correct, 8), ex.submit(correct, 11), ex.submit(verify),
                    ex.submit(verify), ex.submit(correct, 8), ex.submit(verify)]
            results = [f.result() for f in futs]

        # 全部请求得到确定的 200/409，无 500
        self.assertTrue(all(st in (200, 409) for st, _ in results))
        # 最终状态自洽：日量之和 = 单据总量
        _, forms = call("GET", q("/api/forms", branch=b))
        _, daily = call("GET", q("/api/daily", branch=b))
        self.assertEqual(sum(d["volume"] for d in daily), forms[0]["total"])
        self.assertEqual(forms[0]["status"], "effective")


if __name__ == "__main__":
    unittest.main(verbosity=2)
