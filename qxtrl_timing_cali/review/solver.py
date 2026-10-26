# -*- coding: utf-8 -*-
"""
solver.py — 全局求解：把一堆两两相对时序一次性解成"每条线该右移多少"

为什么必须这么算：每次实验都有误差。若像旧流程那样**测一对、立刻写回一对**，
前一步的误差就变成下一步的起点，沿链路一路累加（校 8 比特时能攒出几十 µs 的
无效位移）。这里反过来——测量阶段只收集 skew，所有线的位置由**一次全局加权
最小二乘**同时定出，随机误差按 1/σ² 分摊到整张图上而不是压给最后几步。

数学：设 o_i 为第 i 条线的相对位置（只能确定到一个公共平移）。
每条边给一个观测式约束 o_a − o_b ≈ s_e，权重 w_e = 1/σ_e²。
最小化 Σ w_e (o_a − o_b − s_e)² 的解满足加权图拉普拉斯方程  L·o = c，
其中 c_i = Σ_{e:i=a} w_e s_e − Σ_{e:i=b} w_e s_e。
L 奇异（零空间 = 全体平移），用共轭梯度法在 ⟂1 的子空间内求解即可；
零依赖、纯标准库。

定标：只能右移，所以把公共平移定死——目标取 **当前最靠右的那条线**，
于是每条线的位移 δ_i = max(o) − o_i ≥ 0，且总线位移最小。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable

from .contracts import WritePlan

DEFAULT_SIGMA_FLOOR = 1e-6       # σ=0 时退化出的权重保护
CG_TOL = 1e-12                   # 共轭梯度相对残差收敛门限


@dataclass
class RelativeEdge:
    """一条相对时序观测：skew_ns = q(a) − q(b)，不确定度 sigma_ns。"""

    a: str
    b: str
    skew_ns: float
    sigma_ns: float = 1.0
    request_id: str = ""

    def to_dict(self) -> dict:
        return {"a": self.a, "b": self.b, "skew_ns": self.skew_ns,
                "sigma_ns": self.sigma_ns, "request_id": self.request_id}


@dataclass
class SolveResult:
    """一次全局求解的结果。shifts 逐线恒 ≥ 0，可直接交给回写守卫。

    headroom_return：可选的"收回 headroom"通道（线名 → 收回量，恒 ≥ 0），
    由编排层（stages）在收尾求解时填入；求解器本身永远只产出 δ ≥ 0 的 shifts。
    """

    offsets: dict[str, float]          # 相对位置估计 o_i（右为正 = 时序靠后）
    shifts: dict[str, float]           # 逐线右移量
    residuals: list[dict] = field(default_factory=list)
    target_ns: float = 0.0
    max_resid_ns: float = 0.0
    rms_resid_ns: float = 0.0
    iterations: int = 0
    solver_converged: bool = False
    request_id: str = "solver"
    reason: str = ""
    headroom_return: dict[str, float] = field(default_factory=dict)

    @property
    def covered(self) -> list[str]:
        return sorted(self.offsets)

    @property
    def anchor(self) -> str | None:
        """目标线（初始最右的那条）——它不需要右移。"""
        if not self.shifts and not self.headroom_return:
            return None
        movable = {**self.shifts, **{k: -v for k, v in self.headroom_return.items()}}
        return min(movable, key=lambda ln: (movable[ln], ln))

    def to_dict(self) -> dict:
        return {"target_ns": round(self.target_ns, 6), "anchor": self.anchor,
                "offsets": {k: round(v, 6) for k, v in self.offsets.items()},
                "shifts": dict(self.shifts), "residuals": list(self.residuals),
                "headroom_return": dict(self.headroom_return),
                "max_resid_ns": round(self.max_resid_ns, 6),
                "rms_resid_ns": round(self.rms_resid_ns, 6),
                "iterations": self.iterations, "solver_converged": self.solver_converged,
                "request_id": self.request_id, "reason": self.reason}

    @classmethod
    def from_dict(cls, d: dict) -> "SolveResult":
        """从 to_dict() 还原（步 5 的全局解要随测量日志跨进程取回，供步 6 作软约束）。"""
        d = dict(d or {})
        return cls(offsets={str(k): float(v) for k, v in (d.get("offsets") or {}).items()},
                   shifts={str(k): float(v) for k, v in (d.get("shifts") or {}).items()},
                   residuals=list(d.get("residuals") or []),
                   target_ns=float(d.get("target_ns", 0.0)),
                   max_resid_ns=float(d.get("max_resid_ns", 0.0)),
                   rms_resid_ns=float(d.get("rms_resid_ns", 0.0)),
                   iterations=int(d.get("iterations", 0)),
                   solver_converged=bool(d.get("solver_converged", False)),
                   request_id=str(d.get("request_id", "solver")),
                   reason=str(d.get("reason", "")),
                   headroom_return={str(k): float(v)
                                    for k, v in (d.get("headroom_return") or {}).items()})

    def plan(self, request_id: str | None = None) -> WritePlan:
        """包装成回写计划（不碰同步块——批量流程里已无块概念）。"""
        return WritePlan(request_id=request_id or self.request_id,
                         shifts=dict(self.shifts), merge=None, reason=self.reason,
                         headroom_return=dict(self.headroom_return))

    def require_converged(self) -> None:
        """数值未收敛时禁止推进文件、会话或设备；空解也不算成功。"""
        if not self.offsets or not self.solver_converged:
            raise ValueError("求解为空或未收敛，禁止应用位移")


# ===================== 加权最小二乘 =====================

def _laplacian(entries: list[tuple[int, int, float]], x: list[float]) -> list[float]:
    """(L·x)_i = Σ_{e 与 i 相邻} w_e (x_i − x_j)。"""
    y = [0.0] * len(x)
    for ia, ib, w in entries:
        d = w * (x[ia] - x[ib])
        y[ia] += d
        y[ib] -= d
    return y


def _conjugate_gradient(entries: list[tuple[int, int, float]], rhs: list[float], *,
                        max_iter: int, tol: float = CG_TOL) -> tuple[list[float], int, bool]:
    """解奇异系统 L·x = rhs（rhs ⟂ 1 时相容），返回 (解, 迭代数, 是否收敛)。"""
    n = len(rhs)
    if n == 0:
        return [], 0, True
    x = [0.0] * n
    r = list(rhs)
    p = list(r)
    rs = sum(v * v for v in r)
    if rs == 0.0:                       # 全部 skew 为零：解就是零向量
        return x, 0, True

    scale = math.sqrt(rs)
    for k in range(1, max_iter + 1):
        ap = _laplacian(entries, p)
        denom = sum(pi * ai for pi, ai in zip(p, ap))
        if denom <= 0.0:                # 数值退化或图不连通
            return x, k - 1, False
        alpha = rs / denom
        for i in range(n):
            x[i] += alpha * p[i]
            r[i] -= alpha * ap[i]
        rs_new = sum(v * v for v in r)
        if math.sqrt(rs_new) <= tol * scale:
            return x, k, True
        beta = rs_new / rs
        for i in range(n):
            p[i] = r[i] + beta * p[i]
        rs = rs_new
    return x, max_iter, False


def solve(lines: Iterable[str], edges: Iterable[RelativeEdge], *,
          request_id: str = "solver", max_iter: int | None = None,
          sigma_floor: float = DEFAULT_SIGMA_FLOOR) -> SolveResult:
    """由全部相对时序观测，一次性解出逐线右移量。

    lines 必须已连通（单个分量）；不连通的线请先剔除，它们没有共同基准。
    定标恒取当前最右线（只能右移 ⇒ 位移 δ = max(o) − o ≥ 0 且总量最小）。
    """
    nodes = list(dict.fromkeys(lines))
    edges = list(edges)                 # 观测还要用于残差计算，支持一次性迭代器
    from .graph import components
    if len(components(nodes, [(e.a, e.b) for e in edges])) > 1:
        raise ValueError("solve 只接受单个连通分量")
    if not math.isfinite(sigma_floor) or sigma_floor <= 0:
        raise ValueError("sigma_floor 必须为有限正数")
    if max_iter is not None and max_iter < 1:
        raise ValueError("max_iter 必须为正整数")
    idx = {ln: i for i, ln in enumerate(nodes)}
    entries: list[tuple[int, int, float]] = []
    rhs = [0.0] * len(nodes)

    for e in edges:
        if not math.isfinite(e.skew_ns) or not math.isfinite(e.sigma_ns) or e.sigma_ns < 0:
            raise ValueError("观测 skew 必须有限，sigma 必须有限且非负")
        ia, ib = idx.get(e.a), idx.get(e.b)
        if ia is None or ib is None or ia == ib:
            continue
        w = 1.0 / max(float(e.sigma_ns), sigma_floor) ** 2
        s = float(e.skew_ns)
        entries.append((ia, ib, w))
        rhs[ia] += w * s
        rhs[ib] -= w * s

    x, iters, converged = _conjugate_gradient(
        entries, rhs, max_iter=max_iter or max(200, 20 * len(nodes)))

    if nodes:
        bias = sum(x) / len(nodes)              # 去掉公共平移，取重心为原点
        offsets = {ln: x[i] - bias for ln, i in idx.items()}
    else:
        offsets = {}
    if not offsets:
        return SolveResult(offsets={}, shifts={}, request_id=request_id,
                           reason="无可用相对时序观测")

    target = max(offsets.values())
    shifts = {ln: round(max(0.0, target - o), 6) for ln, o in offsets.items()}

    residuals: list[dict] = []
    for e in edges:
        ia, ib = idx.get(e.a), idx.get(e.b)
        if ia is None or ib is None:
            continue
        resid = offsets[e.a] - offsets[e.b] - float(e.skew_ns)
        residuals.append({"a": e.a, "b": e.b, "skew_ns": round(float(e.skew_ns), 6),
                          "resid_ns": round(resid, 6), "request_id": e.request_id})
    resid_abs = [abs(r["resid_ns"]) for r in residuals] or [0.0]

    return SolveResult(
        offsets={k: round(v, 6) for k, v in offsets.items()},
        shifts=shifts, residuals=residuals, target_ns=round(target, 6),
        max_resid_ns=round(max(resid_abs), 6),
        rms_resid_ns=round(math.sqrt(sum(r * r for r in resid_abs) / len(resid_abs)), 6),
        iterations=iters, solver_converged=converged, request_id=request_id,
        reason=(f"{len(nodes)} 条线 / {len(residuals)} 条观测 · "
                f"目标=最右线 {min(shifts, key=lambda k: (shifts[k], k))} · "
                f"最大残差 {max(resid_abs):.3f} ns"))


def solve_main_component(lines: Iterable[str], edges: Iterable[RelativeEdge], *,
                         request_id: str = "solver", max_iter: int | None = None,
                         sigma_floor: float = DEFAULT_SIGMA_FLOOR) -> SolveResult:
    """只解最大连通分量；两套编排器及恢复路径共用同一隔离规则。"""
    from .graph import components

    nodes, observations = list(dict.fromkeys(lines)), list(edges)
    parts = components(nodes, [(e.a, e.b) for e in observations])
    main = set(max(parts, key=len)) if parts else set()
    if len(main) < 2:
        return SolveResult(offsets={}, shifts={}, request_id=request_id, reason="无可用相对时序观测")
    return solve([n for n in nodes if n in main],
                 [e for e in observations if e.a in main and e.b in main],
                 request_id=request_id, max_iter=max_iter, sigma_floor=sigma_floor)


def solve_components(lines: Iterable[str], edges: Iterable[RelativeEdge], *,
                     request_id: str = "solver", max_iter: int | None = None,
                     sigma_floor: float = DEFAULT_SIGMA_FLOOR) -> SolveResult:
    """按连通分量逐个求解，再把各分量的右移量合成一张计划。

    用途：一组实验里的观测未必把全部线连成一张图（例如读取线组只有"同线 ri↔ro"
    这种边，各条读取线之间暂时没有共同基准）。此时强行做一次全局最小二乘没有意义
    ——不同分量之间根本无从比较。于是每个分量各自定标到**自己最靠右的那条线**，
    δ_i ≥ 0 与"总位移最小"在分量内依旧成立，合并起来仍是一张可直接回写的计划；
    跨分量的那部分失配留给后续实验（例如耦合器组）的桥接边去解。

    注意：多分量时 `offsets` 只在**各自分量内部**可比，跨分量没有共同零点。
    """
    from .graph import components as _components

    edges = list(edges)
    nodes = list(dict.fromkeys(lines))
    parts = _components(nodes, [(e.a, e.b) for e in edges])
    parts = [p for p in parts if len(p) > 1]
    if not parts:
        return SolveResult(offsets={}, shifts={}, request_id=request_id,
                           reason="无可用相对时序观测")

    offsets: dict[str, float] = {}
    shifts: dict[str, float] = {}
    residuals: list[dict] = []
    targets: list[float] = []
    iters, max_res, converged = 0, 0.0, True
    for part in parts:
        keep = {ln for ln in part}
        sub = [e for e in edges if e.a in keep and e.b in keep]
        res = solve(part, sub, request_id=f"{request_id}#{'-'.join(part)}",
                    max_iter=max_iter, sigma_floor=sigma_floor)
        offsets.update(res.offsets)
        shifts.update(res.shifts)
        residuals += res.residuals
        targets.append(res.target_ns)
        iters = max(iters, res.iterations)
        max_res = max(max_res, res.max_resid_ns)
        converged = converged and res.solver_converged

    resid_abs = [abs(r["resid_ns"]) for r in residuals] or [0.0]
    anchor = min(shifts, key=lambda k: (shifts[k], k)) if shifts else None
    return SolveResult(
        offsets={k: round(v, 6) for k, v in offsets.items()},
        shifts=shifts, residuals=residuals, target_ns=round(max(targets), 6),
        max_resid_ns=round(max_res, 6),
        rms_resid_ns=round(math.sqrt(sum(r * r for r in resid_abs) / len(resid_abs)), 6),
        iterations=iters, solver_converged=converged, request_id=request_id,
        reason=(f"{len(nodes)} 条线 / {len(residuals)} 条观测 · {len(parts)} 个分量各自定标"
                + (f" · 目标=各分量最右线（例 {anchor}）" if anchor else "")
                + f" · 最大残差 {max_res:.3f} ns"))


if __name__ == "__main__":  # 自测：三条线链式，δ 应精确复原相对关系
    truth = {"A": 10.0, "B": 34.0, "C": 21.0}
    obs = [RelativeEdge("A", "B", truth["A"] - truth["B"], 0.1),
           RelativeEdge("C", "B", truth["C"] - truth["B"], 0.1)]
    res = solve(["A", "B", "C"], obs)
    for ln in ("A", "B", "C"):
        print(f"{ln}: 相对位置 {res.offsets[ln]:+.3f} → 右移 {res.shifts[ln]:.3f} ns")
    print(f"目标 {res.target_ns:.3f} · 锚线 {res.anchor} · 残差 max={res.max_resid_ns}")
