# -*- coding: utf-8 -*-
"""
policies.py — 结果分析（analyze）与决议策略链

分工：
  * analyze() 只汇总**阈值无关**的事实：结构核对（过期 / 溯源 / 块可解）+ 派生量
    （|skew|、重复极差、写回后累计 d 预测），产出 Assessment；
  * 阈值全部住在策略里（Thresholds 由审查中心在构造策略链时注入），
    每条策略对自己的判据表态，返回 Verdict 或 None（不表态）。

链序：stale → traceability → quality → budget → alignment(noop) → commit
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable

from .contracts import Assessment, ExperimentRequest, ExperimentResult, Verdict, WritePlan
from .session import CalibrationSession

# 执行方自报标志的两级白名单（经验值到位后扩充）：
#   KNOWN —— 已知瑕疵，"测到了但条件不理想"，记录不判废（看 σ / quality 说话）；
#   FATAL —— 测量本身不可信，直接判废；
#   两者之外一律按异常处理：宁可不写，也不把看不懂的结果落盘。
KNOWN_FLAGS = frozenset({"drift", "low_snr", "low_contrast", "crowded"})
FATAL_FLAGS = frozenset({"saturation", "fit_failed", "hardware_error"})


@dataclass
class Thresholds:
    """审查阈值。数值为量级占位，待真实实验经验值替换。"""
    tol_ns: float = 0.5              # 视为已对齐的容差：内则 noop（只合并、不写文件）
    sigma_max_ns: float = 1.0        # 主判据不确定度上限
    quality_min: float = 0.6         # 主评分下限
    skew_max_ns: float = 1500.0      # 物理合理范围（超出视为测量异常）
    spread_max_ns: float = 2.0       # 多次重复结果极差上限（自洽性）
    # 逐线累计右移预算。批量模式一轮写回只需覆盖「各线到最右线的差距」，
    # 故须 ≥ 初始位置 L 的极差；流水线 CLI 默认放宽到 5000。
    d_max_ns: float | None = 2000.0
    max_attempts: int = 3            # 同一申请最多尝试次数


def quality_problems(result: ExperimentResult, th: Thresholds) -> list[str]:
    """质量判据（与 analyze 解耦）：sigma / 评分 / 合理性 / 自报标志 / 重复自洽。

    批量校准在测量阶段就要当场判断一条边能不能用，那时还没有 Assessment，
    所以判据做成可独立调用的函数，策略链与测量台账共用同一份定义。
    """
    bad = []
    # NaN 与任何阈值比较都是 False（sigma>max、quality<min、|skew|>max 全部放行），
    # 必须先挡在有限性上，否则 NaN 结果会一路混进求解与落盘。
    if not all(math.isfinite(v) for v in (result.skew_ns, result.sigma_ns, result.quality)):
        bad.append(f"主判据含非有限数值（skew={result.skew_ns}, "
                   f"sigma={result.sigma_ns}, quality={result.quality}）")
    if result.sigma_ns > th.sigma_max_ns:
        bad.append(f"sigma={result.sigma_ns:.3f}>{th.sigma_max_ns:g}")
    if result.sigma_ns < 0:
        bad.append("sigma 不可为负")
    if not 0 <= result.quality <= 1:
        bad.append("quality 必须在 0–1 内")
    if result.quality < th.quality_min:
        bad.append(f"quality={result.quality:.3f}<{th.quality_min:g}")
    if abs(result.skew_ns) > th.skew_max_ns:
        bad.append(f"|skew|={abs(result.skew_ns):.1f}>{th.skew_max_ns:g}")
    fatal = sorted(set(result.flags) & FATAL_FLAGS)
    unknown = sorted(set(result.flags) - KNOWN_FLAGS - FATAL_FLAGS)
    if fatal:
        bad.append(f"致命标志 {fatal}")
    if unknown:
        bad.append(f"未知标志 {unknown}（未列入 KNOWN_FLAGS / FATAL_FLAGS）")
    skews = [float(r["skew_ns"]) for r in result.repeats] or [result.skew_ns]
    if any(not math.isfinite(v) for v in skews):
        bad.append("重复观测含非有限数值")
    else:
        spread = max(skews) - min(skews)
        if spread > th.spread_max_ns:
            bad.append(f"重复极差 {spread:.3f}>{th.spread_max_ns:g}")
    return bad


@dataclass
class PolicyContext:
    """策略可读的会话侧信息。"""
    session: CalibrationSession
    request: ExperimentRequest | None
    attempts: int = 1          # 该申请已尝试次数（含本次）
    max_attempts: int = 3


# ===================== 分析 =====================

def analyze(result: ExperimentResult, session: CalibrationSession,
            request: ExperimentRequest | None) -> Assessment:
    """把结果对照会话当前状态，产出检查项 + 派生量 + 建议回写计划（精确位移）。"""
    checks: dict[str, dict] = {}

    def add(name: str, ok: bool, detail: str) -> None:
        checks[name] = {"ok": bool(ok), "detail": detail}

    add("stale", result.config_hash == session.config_hash,
        f"结果基于 {result.config_hash}，当前待测文件 {session.config_hash}")
    add("traceable", request is not None and result.kind == request.kind,
        f"未知申请 {result.request_id!r}" if request is None
        else f"kind={result.kind} vs {request.kind}")
    if request is None:
        return Assessment(result=result, request=None, checks=checks)

    intent = request.intent
    blocks_ok = (intent.get("op") == "align" and intent.get("new") in session.blocks
                 and intent.get("ref") in session.blocks)
    add("blocks", blocks_ok,
        f"块齐备（new={intent.get('new')} ref={intent.get('ref')}）" if blocks_ok
        else f"会话中缺块或 intent 非 align：{intent}")

    # 派生量（与阈值无关，策略只比较数字）
    skews = [float(r["skew_ns"]) for r in result.repeats] or [result.skew_ns]
    facts: dict[str, float] = {
        "skew_ns": result.skew_ns,
        "abs_skew_ns": abs(result.skew_ns),
        "sigma_ns": result.sigma_ns,
        "quality": result.quality,
        "spread_ns": round(max(skews) - min(skews), 6),
        "n_repeats": float(len(result.repeats)),
        "projected_max_d": 0.0,
    }
    proposed = None
    if blocks_ok:
        # 精确位移计划（tol=0）：是否落盘由 alignment 策略按容差裁决
        proposed = session.plan_align(intent["new"], intent["ref"], result.skew_ns,
                                      into=intent.get("into"), request_id=result.request_id)
        touched = [line for bid in (intent["new"], intent["ref"], intent.get("into") or intent["ref"])
                   for line in session.blocks.get(bid, [])]
        facts["projected_max_d"] = max(
            (session.delays[ln] + proposed.shifts.get(ln, 0.0) for ln in dict.fromkeys(touched)),
            default=0.0)
    return Assessment(result=result, request=request, checks=checks,
                      proposed=proposed, facts=facts)


# ===================== 策略 =====================

class Policy:
    """策略基类：decide 返回 Verdict 表示裁决，None 表示不表态交下一条。"""
    name = "policy"

    def decide(self, a: Assessment, ctx: PolicyContext) -> Verdict | None:
        raise NotImplementedError


class StaleResultPolicy(Policy):
    name = "stale"

    def decide(self, a: Assessment, ctx: PolicyContext) -> Verdict | None:
        if not a.checks["stale"]["ok"]:
            return Verdict("recheck", f"结果过期：{a.checks['stale']['detail']}",
                           a.result.request_id)
        return None


class TraceabilityPolicy(Policy):
    name = "traceable"

    def decide(self, a: Assessment, ctx: PolicyContext) -> Verdict | None:
        if not a.checks["traceable"]["ok"]:
            return Verdict("escalate", f"结果与申请不一致：{a.checks['traceable']['detail']}",
                           a.result.request_id)
        if not a.checks["blocks"]["ok"]:
            return Verdict("replan", f"无法生成回写计划：{a.checks['blocks']['detail']}",
                           a.result.request_id)
        return None


class QualityPolicy(Policy):
    """精度 / 评分 / 合理性 / 自报标志 / 重复自洽 → 不达标先重试，用尽次数上报。"""
    name = "quality"

    def __init__(self, th: Thresholds):
        self.th = th

    def _bad(self, a: Assessment) -> list[str]:
        return quality_problems(a.result, self.th)

    def decide(self, a: Assessment, ctx: PolicyContext) -> Verdict | None:
        bad = self._bad(a)
        if not bad:
            return None
        detail = "；".join(bad)
        if ctx.attempts >= ctx.max_attempts:
            return Verdict("escalate", f"连续 {ctx.attempts} 次质量不达标：{detail}",
                           a.result.request_id)
        return Verdict("retry", f"质量不达标（第 {ctx.attempts}/{ctx.max_attempts} 次）：{detail}",
                       a.result.request_id)


class BudgetPolicy(Policy):
    name = "budget"

    def __init__(self, th: Thresholds):
        self.th = th

    def decide(self, a: Assessment, ctx: PolicyContext) -> Verdict | None:
        if self.th.d_max_ns is None or a.proposed is None:
            return None
        projected = a.facts["projected_max_d"]
        if projected <= self.th.d_max_ns + 1e-9:
            return None
        return Verdict("escalate",
                       f"写回后累计 d={projected:.1f}ns 超预算 d_max={self.th.d_max_ns:g}ns",
                       a.result.request_id)


class AlignmentPolicy(Policy):
    """偏差已在容差内 → noop：只合并同步块，不占延时预算。"""
    name = "alignment"

    def __init__(self, th: Thresholds):
        self.th = th

    def decide(self, a: Assessment, ctx: PolicyContext) -> Verdict | None:
        plan = a.proposed
        if plan is None or a.facts["abs_skew_ns"] > self.th.tol_ns:
            return None
        merged = WritePlan(request_id=plan.request_id, shifts={}, merge=plan.merge,
                           reason=f"|skew|={a.facts['abs_skew_ns']:.3f}ns ≤ tol="
                                  f"{self.th.tol_ns:g}ns → 仅合并块 {plan.merge}")
        return Verdict("noop", merged.reason, a.result.request_id, write_plan=merged)


class CommitPolicy(Policy):
    """链尾兜底：批准写回。"""
    name = "commit"

    def decide(self, a: Assessment, ctx: PolicyContext) -> Verdict | None:
        if a.proposed is None:
            return Verdict("replan", "无可用回写计划", a.result.request_id)
        return Verdict("commit", f"批准写回：{a.proposed.reason}", a.result.request_id,
                       write_plan=a.proposed)


def default_chain(th: Thresholds) -> list[Policy]:
    return [StaleResultPolicy(), TraceabilityPolicy(), QualityPolicy(th), BudgetPolicy(th),
            AlignmentPolicy(th), CommitPolicy()]
