"""业务规则层：单据登记/核对/补齐/修订/更正重算/预测受理。"""
from __future__ import annotations

import hashlib
import json
from datetime import date

from . import solver
from .storage import (audit, get_conn, mark_rollback_audit, row_to_daily,
                      row_to_form, row_to_forecast, transaction)

MAX_VOLUME = 100_000


class BizError(Exception):
    def __init__(self, status: int, code: str, message: str, extra: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra or {}


# ----------------------------- 校验工具 -----------------------------

def _iso_day(s) -> str:
    if not isinstance(s, str):
        raise BizError(422, "bad_date", "日期必须为 YYYY-MM-DD 字符串")
    try:
        date.fromisoformat(s)
    except ValueError:
        raise BizError(422, "bad_date", f"非法日期：{s}")
    return s


def _nonneg_int(v, name: str, limit=MAX_VOLUME) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or v < 0 or v > limit:
        raise BizError(422, "bad_number", f"{name}必须是 0..{limit} 的非负整数")
    return v


def _validate_form(payload: dict) -> dict:
    branch = payload.get("branch")
    if not isinstance(branch, str) or not branch.strip():
        raise BizError(422, "bad_branch", "网点名称必填")
    start, end = _iso_day(payload.get("start_day")), _iso_day(payload.get("end_day"))
    if start > end:
        raise BizError(422, "bad_range", "起始日不能晚于截止日")
    total = _nonneg_int(payload.get("total"), "总量")
    return {"branch": branch.strip(), "start_day": start, "end_day": end, "total": total}


# ----------------------------- 读取 -----------------------------

def list_forms(branch: str | None = None) -> list[dict]:
    conn = get_conn()
    if branch:
        rows = conn.execute("SELECT * FROM forms WHERE branch=? ORDER BY start_day,id", (branch,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM forms ORDER BY branch,start_day,id").fetchall()
    return [row_to_form(r) for r in rows]


def list_daily(branch: str | None = None) -> list[dict]:
    conn = get_conn()
    if branch:
        rows = conn.execute("SELECT * FROM daily_values WHERE branch=? ORDER BY day", (branch,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM daily_values ORDER BY branch,day").fetchall()
    return [row_to_daily(r) for r in rows]


def list_forecasts(branch: str | None = None) -> list[dict]:
    conn = get_conn()
    if branch:
        rows = conn.execute("SELECT * FROM forecasts WHERE branch=? ORDER BY target_day", (branch,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM forecasts ORDER BY branch,target_day").fetchall()
    return [row_to_forecast(r) for r in rows]


def list_audit() -> list[dict]:
    conn = get_conn()
    return [dict(r) for r in conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 200")]


def _branch_forms(conn, branch: str) -> list[dict]:
    rows = conn.execute("SELECT * FROM forms WHERE branch=? ORDER BY id", (branch,)).fetchall()
    return [row_to_form(r) for r in rows]


def _branch_fixed(conn, branch: str) -> dict[str, int]:
    rows = conn.execute(
        "SELECT day,volume FROM daily_values WHERE branch=? AND source IN ('reported','manual')",
        (branch,)).fetchall()
    return {r["day"]: r["volume"] for r in rows}


def _covering_form_ids(forms: list[dict], day: str) -> list[int]:
    return [f["id"] for f in forms if f["start_day"] <= day <= f["end_day"]]


# ----------------------------- 单据登记 -----------------------------

def create_form(payload: dict) -> dict:
    data = _validate_form(payload)
    with transaction() as conn:
        cur = conn.execute(
            "INSERT INTO forms(branch,start_day,end_day,total,status) VALUES (?,?,?,?,'pending')",
            (data["branch"], data["start_day"], data["end_day"], data["total"]))
        form = row_to_form(conn.execute("SELECT * FROM forms WHERE id=?", (cur.lastrowid,)).fetchone())
        audit("register_form", form)
        return form


# ----------------------------- 求解快照 -----------------------------

def _solve_branch(conn, branch: str) -> dict:
    forms = _branch_forms(conn, branch)
    fixed = _branch_fixed(conn, branch)
    if not forms:
        return {"status": "no_form", "assignment": {}, "uncertain": [], "conflicts": [],
                "direct_conflicts": [], "var_days": [], "nodes": 0}
    return solver.solve(forms, fixed)


def check_branch(branch: str) -> dict:
    """只读核对：返回唯一解/多解/矛盾，不写任何数据。"""
    with transaction() as conn:
        forms = _branch_forms(conn, branch)
        result = _solve_branch(conn, branch)
        result["branch"] = branch
        result["forms"] = forms
        return result


# ----------------------------- 核对生效 + 补齐 -----------------------------

def verify_form(form_id: int) -> dict:
    """待核对 -> 生效；仅当全部缺日有唯一非负整数解时补齐。重试幂等。"""
    with transaction() as conn:
        row = conn.execute("SELECT * FROM forms WHERE id=?", (form_id,)).fetchone()
        if not row:
            raise BizError(404, "not_found", "单据不存在")
        form = row_to_form(row)
        result = _solve_branch(conn, form["branch"])

        if form["status"] == "effective":
            # 幂等重试：不重复补录，返回现状
            return {"form": form, "result": result, "filled": [],
                    "message": "单据已生效，重试未重复补录"}

        if result["status"] == "infeasible":
            mark_rollback_audit("verify_rejected", {"form_id": form_id,
                "conflicts": result["conflicts"],
                "direct_conflicts": result["direct_conflicts"]})
            raise BizError(409, "infeasible", "约束矛盾，无法核对生效",
                           {"conflicts": result["conflicts"],
                            "direct_conflicts": result["direct_conflicts"]})
        if result["status"] == "multiple":
            mark_rollback_audit("verify_rejected",
                {"form_id": form_id, "uncertain": result["uncertain"]})
            raise BizError(409, "multiple", "缺日存在多解，无法核对生效",
                           {"uncertain": result["uncertain"]})

        conn.execute("UPDATE forms SET status='effective',version=version+1 "
                     "WHERE branch=? AND status='pending'", (form["branch"],))
        forms = _branch_forms(conn, form["branch"])
        filled = _apply_derived(conn, form["branch"], result, forms)
        form = row_to_form(conn.execute("SELECT * FROM forms WHERE id=?", (form_id,)).fetchone())
        audit("verify_form", {"form_id": form_id, "branch": form["branch"], "filled": filled})
        return {"form": form, "result": result, "filled": filled}


def _apply_derived(conn, branch: str, result: dict, forms: list[dict]) -> list[dict]:
    """按唯一解补齐缺日；已有日量不改；INSERT OR IGNORE 保证重试不重复补录。"""
    filled = []
    for day, volume in sorted(result["assignment"].items()):
        exists = conn.execute("SELECT 1 FROM daily_values WHERE branch=? AND day=?",
                              (branch, day)).fetchone()
        if exists:
            continue  # 已有日量不改
        src_ids = _covering_form_ids(forms, day)
        cur = conn.execute(
            "INSERT OR IGNORE INTO daily_values(branch,day,volume,source,source_form_ids) "
            "VALUES (?,?,?, 'derived', ?)",
            (branch, day, int(volume), json.dumps(src_ids)))
        if cur.rowcount == 1:
            filled.append({"day": day, "volume": int(volume), "source_form_ids": src_ids})
    return filled


# ----------------------------- 日结上报 / 人工修订 -----------------------------

def upsert_daily(branch: str, day: str, volume: int, source: str, note: str = "") -> dict:
    """日结上报(reported)与人工修订(manual)：固定值，参与约束校验。

    已有日量不改的原则只针对“补齐”动作；这里是显式修订，需整套重算，
    约束失败（矛盾）则保留原数据并拒绝。"""
    _iso_day(day)
    volume = _nonneg_int(volume, "日量")
    if source not in ("reported", "manual"):
        raise BizError(422, "bad_source", "来源只能是 reported 或 manual")
    with transaction() as conn:
        old = conn.execute("SELECT * FROM daily_values WHERE branch=? AND day=?",
                           (branch, day)).fetchone()
        old_data = row_to_daily(old) if old else None

        # 试写：先在影子状态求解（通过临时更新后回滚判断）
        if old:
            conn.execute("UPDATE daily_values SET volume=?,source=?,source_form_ids='[]',"
                         "updated_at=datetime('now') WHERE branch=? AND day=?",
                         (volume, source, branch, day))
        else:
            conn.execute("INSERT INTO daily_values(branch,day,volume,source,source_form_ids) "
                         "VALUES (?,?,?,?,'[]')", (branch, day, volume, source))

        result = _solve_branch(conn, branch)
        if result["status"] == "infeasible":
            mark_rollback_audit("upsert_rejected", {"branch": branch, "day": day,
                "volume": volume, "source": source,
                "conflicts": result["conflicts"],
                "direct_conflicts": result["direct_conflicts"], "old": old_data})
            raise BizError(409, "infeasible", "修订后与区间单据约束矛盾，已保留原数据",
                           {"conflicts": result["conflicts"],
                            "direct_conflicts": result["direct_conflicts"], "old": old_data})

        # 仅当该网点已有生效单据（核对过）时，修订才连同原补齐值整套重算；
        # 全部待核对阶段不提前补齐，补齐只发生在核对生效且解唯一时。
        if any(f["status"] == "effective" for f in _branch_forms(conn, branch)):
            _rebuild_derived(conn, branch, result)
        row = conn.execute("SELECT * FROM daily_values WHERE branch=? AND day=?",
                           (branch, day)).fetchone()
        audit("upsert_daily", {"branch": branch, "day": day, "volume": volume,
                               "source": source, "note": note, "old": old_data,
                               "uncertain": result.get("uncertain", [])})
        return row_to_daily(row)


def _rebuild_derived(conn, branch: str, result: dict) -> list[dict]:
    """删除该网点所有 derived 行后按当前解重建；多解仅重建唯一确定的日期。"""
    conn.execute("DELETE FROM daily_values WHERE branch=? AND source='derived'", (branch,))
    if result["status"] not in ("unique", "multiple"):
        return []
    forms = _branch_forms(conn, branch)
    filled = []
    if result["status"] == "unique":
        assignment = result["assignment"]
    else:
        # 多解：取所有解中取值唯一的变量（求解器已在 uncertain 中给出不唯一项）
        assignment = {d: v for d, v in result["assignment"].items()
                      if d not in set(result["uncertain"])}
    for day, volume in sorted(assignment.items()):
        src_ids = _covering_form_ids(forms, day)
        conn.execute(
            "INSERT INTO daily_values(branch,day,volume,source,source_form_ids) "
            "VALUES (?,?,?, 'derived', ?)",
            (branch, day, int(volume), json.dumps(src_ids)))
        filled.append({"day": day, "volume": int(volume), "source_form_ids": src_ids})
    return filled


# ----------------------------- 生效单据更正 -----------------------------

def correct_form(form_id: int, payload: dict) -> dict:
    """生效单据更正：连同原补齐值整套重算；失败保留原数据。"""
    data = _validate_form(payload)
    with transaction() as conn:
        row = conn.execute("SELECT * FROM forms WHERE id=?", (form_id,)).fetchone()
        if not row:
            raise BizError(404, "not_found", "单据不存在")
        old_form = row_to_form(row)

        conn.execute("UPDATE forms SET branch=?,start_day=?,end_day=?,total=? WHERE id=?",
                     (data["branch"], data["start_day"], data["end_day"], data["total"], form_id))
        result = _solve_branch(conn, data["branch"])
        if result["status"] == "infeasible":
            mark_rollback_audit("correct_rejected", {"form_id": form_id,
                "conflicts": result["conflicts"],
                "direct_conflicts": result["direct_conflicts"], "old": old_form})
            raise BizError(409, "infeasible", "更正后约束矛盾，已保留原数据",
                           {"conflicts": result["conflicts"],
                            "direct_conflicts": result["direct_conflicts"], "old": old_form})
        if result["status"] == "multiple":
            mark_rollback_audit("correct_rejected",
                {"form_id": form_id, "uncertain": result["uncertain"], "old": old_form})
            raise BizError(409, "multiple", "更正后缺日出现多解，已保留原数据",
                           {"uncertain": result["uncertain"], "old": old_form})

        # 唯一解：版本+1，重建该网点全部补齐值（原补齐值连同重算）
        conn.execute("UPDATE forms SET version=version+1 WHERE id=?", (form_id,))
        filled = _rebuild_derived(conn, data["branch"], result)
        form = row_to_form(conn.execute("SELECT * FROM forms WHERE id=?", (form_id,)).fetchone())
        audit("correct_form", {"form_id": form_id, "old": old_form, "new": form, "filled": filled})
        return {"form": form, "result": result, "filled": filled}


# ----------------------------- 预测受理 -----------------------------

def _forecast_basis(conn, branch: str, target_day: str) -> dict:
    forms = [f for f in _branch_forms(conn, branch) if f["status"] == "effective"]
    fixed_rows = conn.execute(
        "SELECT day,volume,source FROM daily_values WHERE branch=? "
        "AND source IN ('reported','manual') ORDER BY day", (branch,)).fetchall()
    fixed = [dict(r) for r in fixed_rows]
    return {"branch": branch, "target_day": target_day,
            "effective_forms": forms, "fixed_daily": fixed}


def _basis_hash(basis: dict) -> str:
    raw = json.dumps(basis, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def accept_forecast(branch: str, target_day: str) -> dict:
    """预测受理：待核对单据覆盖目标日则阻止；旧预测一律保留原值。"""
    _iso_day(target_day)
    with transaction() as conn:
        if not _branch_forms(conn, branch):
            raise BizError(404, "not_found", "该网点尚无单据，无法受理预测")
        pending = conn.execute(
            "SELECT id FROM forms WHERE branch=? AND status='pending' "
            "AND start_day<=? AND end_day>=?", (branch, target_day, target_day)).fetchall()
        if pending:
            ids = [r["id"] for r in pending]
            mark_rollback_audit("forecast_blocked",
                {"branch": branch, "target_day": target_day, "forms": ids})
            raise BizError(409, "pending_blocks_forecast",
                           "存在覆盖该日期的待核对单据，未核对部分阻止预测", {"forms": ids})

        old = conn.execute("SELECT * FROM forecasts WHERE branch=? AND target_day=?",
                           (branch, target_day)).fetchone()
        basis = _forecast_basis(conn, branch, target_day)
        h = _basis_hash(basis)
        if old:
            kept = row_to_forecast(old)
            if kept["basis_hash"] != h:
                conn.execute("UPDATE forecasts SET retained=1 WHERE id=?", (old["id"],))
                kept["retained"] = True
                audit("forecast_retained", {"id": old["id"], "old_hash": kept["basis_hash"],
                                            "new_hash": h})
            return {"forecast": kept, "created": False,
                    "message": "旧预测保留原值，未重新受理"}

        if not basis["fixed_daily"]:
            raise BizError(422, "no_basis", "缺少已确认日量，无法计算预测")
        # 朴素预测：以全部已确认日量的均值四舍五入作为目标日预测
        avg = sum(r["volume"] for r in basis["fixed_daily"]) / len(basis["fixed_daily"])
        value = int(avg + 0.5)
        cur = conn.execute(
            "INSERT INTO forecasts(branch,target_day,value,basis_json,basis_hash,retained) "
            "VALUES (?,?,?,?,?,0)",
            (branch, target_day, value, json.dumps(basis, ensure_ascii=False), h))
        forecast = row_to_forecast(
            conn.execute("SELECT * FROM forecasts WHERE id=?", (cur.lastrowid,)).fetchone())
        audit("forecast_accepted", {"id": cur.lastrowid, "branch": branch,
                                     "target_day": target_day, "value": value})
        return {"forecast": forecast, "created": True}
