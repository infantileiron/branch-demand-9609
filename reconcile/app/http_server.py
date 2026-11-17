"""零依赖 HTTP 服务：JSON API + 静态前端，同源部署。"""
from __future__ import annotations

import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from . import service
from .service import BizError

STATIC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "web"))
MIME = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml"}


class Handler(BaseHTTPRequestHandler):
    server_version = "Reconcile/1.0"

    def log_message(self, fmt, *args):  # 安静一点
        pass

    # ---------- 基础收发 ----------
    def _send(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise BizError(400, "bad_json", "请求体不是合法 JSON")
        if not isinstance(data, dict):
            raise BizError(400, "bad_body", "请求体必须是 JSON 对象")
        return data

    def _query(self) -> dict:
        q = parse_qs(urlparse(self.path).query)
        return {k: v[0] for k, v in q.items()}

    # ---------- 路由 ----------
    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        try:
            if path == "/health":
                return self._send(200, {"status": "ok"})
            if path == "/api/forms":
                return self._send(200, service.list_forms(self._query().get("branch")))
            if path == "/api/daily":
                return self._send(200, service.list_daily(self._query().get("branch")))
            if path == "/api/forecasts":
                return self._send(200, service.list_forecasts(self._query().get("branch")))
            if path == "/api/audit":
                return self._send(200, service.list_audit())
            m = re.fullmatch(r"/api/branches/([^/]+)/check", path)
            if m:
                return self._send(200, service.check_branch(m.group(1)))
            return self._static(path)
        except BizError as e:
            return self._biz_error(e)
        except Exception as e:  # noqa: BLE001
            return self._send(500, {"code": "internal", "detail": "服务暂时异常：" + str(e)})

    def do_POST(self):
        path = unquote(urlparse(self.path).path)
        try:
            body = self._body()
            if path == "/api/forms":
                return self._send(201, service.create_form(body))
            if path == "/api/daily":
                rec = service.upsert_daily(
                    body.get("branch"), body.get("day"), body.get("volume"),
                    body.get("source", "reported"), body.get("note", ""))
                return self._send(200, rec)
            m = re.fullmatch(r"/api/forms/(\d+)/verify", path)
            if m:
                return self._send(200, service.verify_form(int(m.group(1))))
            if path == "/api/forecasts":
                rec = service.accept_forecast(body.get("branch"), body.get("target_day"))
                return self._send(200, rec)
            return self._send(404, {"code": "not_found", "detail": "未知接口"})
        except BizError as e:
            return self._biz_error(e)
        except Exception as e:  # noqa: BLE001
            return self._send(500, {"code": "internal", "detail": "服务暂时异常：" + str(e)})

    def do_PUT(self):
        path = unquote(urlparse(self.path).path)
        try:
            body = self._body()
            m = re.fullmatch(r"/api/forms/(\d+)", path)
            if m:
                return self._send(200, service.correct_form(int(m.group(1)), body))
            return self._send(404, {"code": "not_found", "detail": "未知接口"})
        except BizError as e:
            return self._biz_error(e)
        except Exception as e:  # noqa: BLE001
            return self._send(500, {"code": "internal", "detail": "服务暂时异常：" + str(e)})

    def _biz_error(self, e: BizError) -> None:
        self._send(e.status, {"code": e.code, "detail": e.message, **e.extra})

    def _static(self, path: str) -> None:
        if path in ("", "/"):
            path = "/index.html"
        target = os.path.normpath(os.path.join(STATIC_DIR, path.lstrip("/")))
        if not target.startswith(STATIC_DIR) or not os.path.isfile(target):
            return self._send(404, {"code": "not_found", "detail": "页面不存在"})
        ext = os.path.splitext(target)[1]
        with open(target, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run(host: str = "0.0.0.0", port: int = 18309) -> None:
    service.get_conn()
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"对账服务已启动: http://localhost:{port}")
    httpd.serve_forever()


if __name__ == "__main__":
    run(port=int(os.environ.get("PORT", "18309")))
