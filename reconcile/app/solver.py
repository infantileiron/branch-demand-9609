"""缺日求解器。

每个区间汇总单是一条方程：
    sum(区间内日量) = 总量
已有日量（日结上报 / 人工修订）作为固定常量，缺日为非负整数变量。
交叠单据的方程共同约束同一批变量。

结果三类：
  infeasible 无解   -> 列出冲突单据
  unique     唯一解 -> 可补齐
  multiple   多解   -> 列出取值不唯一（仍不确定）的日期
"""
from __future__ import annotations

from datetime import date, timedelta

SOL_CAP = 500          # 枚举解上限
NODE_CAP = 200_000     # 搜索节点上限，触顶后剩余变量一律视为不确定（保守不补）


def daterange(start: str, end: str) -> list[str]:
    d1 = date.fromisoformat(start)
    d2 = date.fromisoformat(end)
    if d1 > d2:
        raise ValueError("起始日晚于截止日")
    out, cur = [], d1
    while cur <= d2:
        out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


def _build(forms: list[dict], fixed: dict[str, int]):
    """构造变量与方程。返回 (var_days, equations) 或 None（基础校验失败）。"""
    covered: set[str] = set()
    parsed = []
    for f in forms:
        days = daterange(f["start_day"], f["end_day"])
        parsed.append((f["id"], days, int(f["total"])))
        covered.update(days)
    var_days = sorted(d for d in covered if d not in fixed)
    idx = {d: i for i, d in enumerate(var_days)}
    equations = []
    for fid, days, total in parsed:
        var_idx = [idx[d] for d in days if d in idx]
        fixed_sum = sum(fixed[d] for d in days if d in fixed)
        equations.append({"form_id": fid, "vars": var_idx,
                          "fixed_sum": fixed_sum, "total": total})
    return var_days, equations


def _propagate(val: list[int], hi: list[int], equations: list[dict]):
    """区间传播：收紧上界、强制归零。返回 False 即不可行。原地修改 val/hi。"""
    changed = True
    while changed:
        changed = False
        for eq in equations:
            rem = eq["total"] - eq["fixed_sum"]
            free = []
            for v in eq["vars"]:
                if val[v] >= 0:
                    rem -= val[v]
                else:
                    free.append(v)
            if rem < 0:
                return False
            if not free:
                if rem != 0:
                    return False
                continue
            if rem == 0:
                for v in free:
                    if val[v] != 0:
                        val[v] = 0
                        changed = True
                continue
            cap_max = 0
            for v in free:
                if hi[v] > rem:
                    hi[v] = rem
                    changed = True
                cap_max += hi[v]
            if cap_max < rem:
                return False
    return True


def _search(var_days, equations, limit_solutions: int | None = None):
    n = len(var_days)
    val = [-1] * n
    hi: list[int] = []
    for i in range(n):
        bound = min((eq["total"] - eq["fixed_sum"])
                    for eq in equations if i in eq["vars"])
        if bound < 0:
            return {"feasible": False, "solutions": [], "node_capped": False, "nodes": 0}
        hi.append(bound)
    if not _propagate(val, hi, equations):
        return {"feasible": False, "solutions": [], "node_capped": False, "nodes": 0}

    solutions: list[tuple[int, ...]] = []
    state = {"nodes": 0, "capped": False}
    sol_cap = SOL_CAP if limit_solutions is None else limit_solutions

    def dfs() -> None:
        state["nodes"] += 1
        if state["nodes"] > NODE_CAP:
            state["capped"] = True
            return
        if len(solutions) >= sol_cap or state["capped"]:
            return
        unassigned = [i for i in range(n) if val[i] < 0]
        if not unassigned:
            solutions.append(tuple(val))
            return
        # 选域最小的变量；平手选出现在方程最多者（传播更强）
        v = min(unassigned, key=lambda i: (hi[i],
                -sum(1 for eq in equations if i in eq["vars"])))
        for candidate in range(hi[v] + 1):
            snap_val, snap_hi = val[:], hi[:]
            val[v] = candidate
            if _propagate(val, hi, equations):
                dfs()
            val[:], hi[:] = snap_val, snap_hi
            if len(solutions) >= sol_cap or state["capped"]:
                return

    dfs()
    return {"feasible": bool(solutions), "solutions": solutions,
            "node_capped": state["capped"], "nodes": state["nodes"]}


def _is_feasible(forms: list[dict], fixed: dict[str, int]) -> bool:
    built = _build(forms, fixed)
    if built is None:
        return False
    var_days, equations = built
    return _search(var_days, equations, limit_solutions=1)["feasible"]


def solve(forms: list[dict], fixed: dict[str, int]) -> dict:
    """主入口。forms 为参与约束的全部单据（待核对+生效）。"""
    var_days, equations = _build(forms, fixed)

    # 1) 直接冲突：固定值之和已超总量
    direct = [eq["form_id"] for eq in equations
              if eq["fixed_sum"] > eq["total"]]
    if direct:
        return {"status": "infeasible", "assignment": {}, "uncertain": [],
                "conflicts": sorted(set(direct)), "direct_conflicts": sorted(set(direct)),
                "var_days": var_days, "nodes": 0}

    result = _search(var_days, equations)
    if not result["feasible"]:
        # 2) 删除过滤求一个极小不可行单据组（IIS）
        remaining = [f["id"] for f in forms]
        alive = {f["id"]: f for f in forms}
        for fid in list(remaining):
            trial = [alive[x] for x in remaining if x != fid]
            if not _is_feasible(trial, fixed):
                remaining.remove(fid)
                alive.pop(fid)
        return {"status": "infeasible", "assignment": {}, "uncertain": [],
                "conflicts": sorted(remaining), "direct_conflicts": [],
                "var_days": var_days, "nodes": result["nodes"]}

    solutions = result["solutions"]
    first = dict(zip(var_days, solutions[0]))
    value_sets = [set() for _ in var_days]
    for sol in solutions:
        for i, x in enumerate(sol):
            value_sets[i].add(x)

    if len(solutions) == 1 and not result["node_capped"]:
        return {"status": "unique", "assignment": first, "uncertain": [],
                "conflicts": [], "direct_conflicts": [],
                "var_days": var_days, "nodes": result["nodes"]}

    uncertain = [d for d, vals in zip(var_days, value_sets) if len(vals) > 1]
    if result["node_capped"]:
        # 搜索触顶：未观察到分歧的变量也保守列为不确定
        uncertain = [d for d, vals in zip(var_days, value_sets)]
    return {"status": "multiple", "assignment": first,
            "uncertain": sorted(set(uncertain)),
            "conflicts": [], "direct_conflicts": [],
            "var_days": var_days, "nodes": result["nodes"],
            "node_capped": result["node_capped"]}
