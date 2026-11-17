"""SQLite 存储层。单进程内加写锁，事务串行化，满足并发争抢语义。"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager

DB_PATH = os.environ.get("RECONCILE_DB", os.path.join(os.path.dirname(__file__), "..", "data.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS forms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    branch TEXT NOT NULL,
    start_day TEXT NOT NULL,
    end_day TEXT NOT NULL,
    total INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending / effective
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS daily_values (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    branch TEXT NOT NULL,
    day TEXT NOT NULL,
    volume INTEGER NOT NULL,
    source TEXT NOT NULL,                     -- reported / manual / derived
    source_form_ids TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(branch, day)
);
CREATE TABLE IF NOT EXISTS forecasts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    branch TEXT NOT NULL,
    target_day TEXT NOT NULL,
    value INTEGER NOT NULL,
    basis_json TEXT NOT NULL,
    basis_hash TEXT NOT NULL,
    retained INTEGER NOT NULL DEFAULT 0,      -- 1 表示旧预测被保留，未重新受理
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(branch, target_day)
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,
    detail TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_write_lock = threading.RLock()
_rollback_hooks: dict[int, list] = {}


def connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


_conn: sqlite3.Connection | None = None


def get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = connect()
        _conn.executescript(SCHEMA)
    return _conn


@contextmanager
def transaction():
    """串行化写事务：同一进程内所有更正/补齐/预测互斥，保证整套一致依据。

    回滚后仍需保留的审计（如拒绝事件）可通过 mark_rollback_audit 登记。
    """
    conn = get_conn()
    with _write_lock:
        conn.execute("BEGIN IMMEDIATE")
        _rollback_hooks[id(conn)] = []
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            hooks = _rollback_hooks.pop(id(conn), [])
            for action, detail in hooks:
                audit(action, detail)
            raise
        else:
            _rollback_hooks.pop(id(conn), None)


def mark_rollback_audit(action: str, detail: dict) -> None:
    """登记一条仅在当前事务回滚时才写入的审计。"""
    conn = get_conn()
    _rollback_hooks.setdefault(id(conn), []).append((action, detail))


def audit(action: str, detail: dict | list | str) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT INTO audit_log(action, detail) VALUES (?, ?)",
        (action, json.dumps(detail, ensure_ascii=False)),
    )


def row_to_form(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"],
        "branch": r["branch"],
        "start_day": r["start_day"],
        "end_day": r["end_day"],
        "total": r["total"],
        "status": r["status"],
        "version": r["version"],
        "created_at": r["created_at"],
    }


def row_to_daily(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"],
        "branch": r["branch"],
        "day": r["day"],
        "volume": r["volume"],
        "source": r["source"],
        "source_form_ids": json.loads(r["source_form_ids"]),
        "updated_at": r["updated_at"],
    }


def row_to_forecast(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"],
        "branch": r["branch"],
        "target_day": r["target_day"],
        "value": r["value"],
        "basis": json.loads(r["basis_json"]),
        "basis_hash": r["basis_hash"],
        "retained": bool(r["retained"]),
        "created_at": r["created_at"],
    }
