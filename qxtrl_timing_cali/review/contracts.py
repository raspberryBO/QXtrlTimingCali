# -*- coding: utf-8 -*-
"""
contracts.py — 审查中心 ⇄ 执行方之间的数据契约（五种消息）

  ExperimentRequest  申请实验（审查中心 → 执行方）
  ExperimentResult   实验结果（执行方 → 审查中心，字段分必填 / 可选）
  Assessment         分析结论（内部：逐项检查 + 建议回写计划）
  Verdict            决议（commit | noop | retry | recheck | replan | escalate）
  WritePlan          回写计划（纯数据：逐线右移量 + 同步块合并）

设计约束：
  * 全部可 JSON 序列化 —— 文件队列 / 跨进程对接只需 to_dict / from_dict；
  * from_dict 严格校验必填字段，可选字段缺失即取默认值；
  * 执行方只读 targets 与 params，intent（块合并意图）是审查中心内部信息；
  * skew_ns 符号约定：skew = q(new) − q(ref)，new = targets[0]、ref = targets[1]。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

SCHEMA_VERSION = "1.0"

# 决议动作枚举
ACTIONS = ("commit", "noop", "retry", "recheck", "replan", "escalate")
# 允许流水线向前推进的动作（noop = 不写文件但块内已对齐）
ADVANCE = frozenset({"commit", "noop"})


# ===================== 校验小工具 =====================

def _require(d: dict, key: str, types: tuple, where: str) -> Any:
    if key not in d:
        raise ValueError(f"{where}: 缺少必填字段 {key!r}")
    v = d[key]
    if types and not isinstance(v, types):
        raise ValueError(f"{where}: 字段 {key!r} 类型应为 {types}（当前 {type(v).__name__}）")
    if isinstance(v, bool) and (int in types or float in types):
        raise ValueError(f"{where}: 字段 {key!r} 不接受布尔值充当数值")
    return v


def _lines(value: Any, where: str) -> list[str]:
    out = list(value)
    if not out or any(not isinstance(x, str) for x in out):
        raise ValueError(f"{where}: 需要非空字符串列表")
    if len(out) < 2:
        raise ValueError(f"{where}: targets 至少需要两条线（new 与 ref），当前 {len(out)} 条")
    return out


def _finite(value: float, key: str, where: str) -> float:
    """NaN 与任何数比较都是 False，会无声穿透全部阈值/守卫判据——在契约入口就拒收。"""
    if not math.isfinite(value):
        raise ValueError(f"{where}: 字段 {key!r} 不是有限数值（{value}）——NaN/Inf 结果不可采信")
    return value


# ===================== 1. 实验申请 =====================

@dataclass
class ExperimentRequest:
    """一次实验申请：要比较哪两条线（targets[0]=new, targets[1]=ref）。"""
    id: str
    kind: str                          # "timing.align" | "timing.edge_compare" | ...
    targets: list[str]
    config_hash: str                   # 申请时待测文件的指纹（执行方须在此版本上测量）
    intent: dict = field(default_factory=dict)   # {"op","new","ref","into"} 审查中心内部信息
    params: dict = field(default_factory=dict)   # n_shots / averages / timeout_s ...
    note: str = ""

    def to_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "targets": list(self.targets),
                "config_hash": self.config_hash, "intent": dict(self.intent),
                "params": dict(self.params), "note": self.note}

    @classmethod
    def from_dict(cls, d: dict) -> "ExperimentRequest":
        w = "ExperimentRequest"
        return cls(id=_require(d, "id", (str,), w), kind=_require(d, "kind", (str,), w),
                   targets=_lines(_require(d, "targets", (list,), w), w),
                   config_hash=_require(d, "config_hash", (str,), w),
                   intent=dict(d.get("intent") or {}), params=dict(d.get("params") or {}),
                   note=str(d.get("note", "")))

    @property
    def new_line(self) -> str:
        return self.targets[0]

    @property
    def ref_line(self) -> str:
        return self.targets[1]


# ===================== 2. 实验结果 =====================

@dataclass
class ExperimentResult:
    """实验结果契约。必填 = 主判据 + 溯源；可选 = 执行方自报与证据。

    必填：request_id / kind / config_hash / measured_at / skew_ns / sigma_ns / quality
    可选：flags / n_shots / repeats / artifacts / extra
    """
    request_id: str
    kind: str
    config_hash: str                   # 测量所依据的待测文件版本（用于拒绝过期结果）
    measured_at: str                   # ISO 时间戳
    skew_ns: float                     # 主判据：q(new) − q(ref)
    sigma_ns: float                    # 不确定度（标准误）
    quality: float                     # 主评分 0–1
    # ---- 可选 ----
    flags: list[str] = field(default_factory=list)     # 执行方自报异常（saturation/drift…）
    n_shots: int | None = None
    repeats: list[dict] = field(default_factory=list)  # [{skew_ns, sigma_ns, quality}]
    artifacts: dict = field(default_factory=dict)       # {raw, plot, sha256}：路径 + 哈希
    extra: dict = field(default_factory=dict)

    @property
    def abs_skew(self) -> float:
        return abs(self.skew_ns)

    def to_dict(self) -> dict:
        return {"request_id": self.request_id, "kind": self.kind,
                "config_hash": self.config_hash, "measured_at": self.measured_at,
                "skew_ns": self.skew_ns, "sigma_ns": self.sigma_ns, "quality": self.quality,
                "flags": list(self.flags), "n_shots": self.n_shots,
                "repeats": [dict(r) for r in self.repeats],
                "artifacts": dict(self.artifacts), "extra": dict(self.extra)}

    @classmethod
    def from_dict(cls, d: dict) -> "ExperimentResult":
        w = "ExperimentResult"
        num = (int, float)
        sigma = _finite(float(_require(d, "sigma_ns", num, w)), "sigma_ns", w)
        quality = _finite(float(_require(d, "quality", num, w)), "quality", w)
        if sigma < 0 or not 0 <= quality <= 1:
            raise ValueError(f"{w}: sigma 必须非负，quality 必须在 0–1 内")
        reps = list(d.get("repeats") or [])
        for r in reps:
            _finite(float(_require(r, "skew_ns", num, f"{w}.repeats")), "skew_ns", f"{w}.repeats")
        return cls(
            request_id=_require(d, "request_id", (str,), w),
            kind=_require(d, "kind", (str,), w),
            config_hash=_require(d, "config_hash", (str,), w),
            measured_at=_require(d, "measured_at", (str,), w),
            skew_ns=_finite(float(_require(d, "skew_ns", num, w)), "skew_ns", w),
            sigma_ns=sigma,
            quality=quality,
            flags=[str(x) for x in (d.get("flags") or [])],
            n_shots=(int(d["n_shots"]) if d.get("n_shots") is not None else None),
            repeats=[dict(r) for r in reps],
            artifacts=dict(d.get("artifacts") or {}),
            extra=dict(d.get("extra") or {}))


# ===================== 3. 回写计划 =====================

@dataclass
class WritePlan:
    """回写计划（纯数据，可先检查后执行）。

    shifts        : 线名 → 右移量（ns，恒 ≥ 0，只增不减）
    headroom_return: 线名 → 收回量（ns，恒 ≥ 0）。仅用于收回**本流水线先前授予**的
                    readout headroom——校准位移本身仍然只增不减，收回量由会话侧
                    授予台账约束（不得超过该线尚未收回的 headroom 余额）。
    merge         : 同步块合并 {"new","ref","into"}；skew 落在容差内时只做合并不位移
    """
    request_id: str
    shifts: dict[str, float] = field(default_factory=dict)
    merge: dict | None = None
    reason: str = ""
    headroom_return: dict[str, float] = field(default_factory=dict)

    @property
    def total_ns(self) -> float:
        return sum(self.shifts.values())

    @property
    def needs_write(self) -> bool:
        return (any(v > 0.0 for v in self.shifts.values())
                or any(v > 0.0 for v in self.headroom_return.values()))

    def to_dict(self) -> dict:
        return {"request_id": self.request_id, "shifts": dict(self.shifts),
                "merge": dict(self.merge) if self.merge else None, "reason": self.reason,
                "headroom_return": dict(self.headroom_return)}

    @classmethod
    def from_dict(cls, d: dict) -> "WritePlan":
        w = "WritePlan"
        shifts = {str(k): _finite(float(v), f"shifts[{k}]", w)
                  for k, v in (d.get("shifts") or {}).items()}
        if any(v < 0 for v in shifts.values()):
            raise ValueError(f"{w}: 右移量不可为负")
        ret = {str(k): _finite(float(v), f"headroom_return[{k}]", w)
               for k, v in (d.get("headroom_return") or {}).items()}
        if any(v < 0 for v in ret.values()):
            raise ValueError(f"{w}: headroom 收回量不可为负")
        merge = d.get("merge")
        return cls(request_id=_require(d, "request_id", (str,), w), shifts=shifts,
                   merge=dict(merge) if merge else None, reason=str(d.get("reason", "")),
                   headroom_return=ret)


# ===================== 4. 分析结论 =====================

@dataclass
class Assessment:
    """analyze() 的产物：结构核对 + 派生量 + 建议回写计划（不含裁决）。

    checks 只覆盖与阈值无关的事实（过期 / 溯源 / 块可解）；
    facts 是供策略比较的数字（|skew|、sigma、quality、重复极差、写回后累计 d 预测）。
    """
    result: ExperimentResult
    request: ExperimentRequest | None
    checks: dict[str, dict] = field(default_factory=dict)   # 名 → {ok, detail}
    facts: dict[str, float] = field(default_factory=dict)
    proposed: WritePlan | None = None

    @property
    def ok(self) -> bool:
        """结构核对是否全部通过（阈值判据不在此处）。"""
        return all(c["ok"] for c in self.checks.values())

    @property
    def skew_abs(self) -> float:
        return self.facts.get("abs_skew_ns", self.result.abs_skew)

    @property
    def failures(self) -> list[str]:
        return [f"{k}: {c['detail']}" for k, c in self.checks.items() if not c["ok"]]

    def to_dict(self) -> dict:
        return {"request_id": self.result.request_id, "checks": dict(self.checks),
                "ok": self.ok, "facts": dict(self.facts),
                "proposed": self.proposed.to_dict() if self.proposed else None}


# ===================== 5. 决议 =====================

@dataclass
class Verdict:
    """审查决议。action ∈ ACTIONS；commit 时 write_plan 将被守卫式落盘。"""
    action: str
    reason: str
    request_id: str = ""
    write_plan: WritePlan | None = None

    def __post_init__(self) -> None:
        if self.action not in ACTIONS:
            raise ValueError(f"Verdict.action 应为 {ACTIONS} 之一（当前 {self.action!r}）")

    @property
    def accepted(self) -> bool:
        """是否允许流水线推进（commit / noop）。"""
        return self.action in ADVANCE

    def to_dict(self) -> dict:
        return {"action": self.action, "reason": self.reason, "request_id": self.request_id,
                "write_plan": self.write_plan.to_dict() if self.write_plan else None}

    @classmethod
    def from_dict(cls, d: dict) -> "Verdict":
        w = "Verdict"
        plan = d.get("write_plan")
        return cls(action=_require(d, "action", (str,), w), reason=str(d.get("reason", "")),
                   request_id=str(d.get("request_id", "")),
                   write_plan=WritePlan.from_dict(plan) if plan else None)
