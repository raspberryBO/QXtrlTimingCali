# -*- coding: utf-8 -*-
"""
center.py — 审查中心（自动校准端的调度核心）

四项技能（智能编排为后期能力，此处只提供固定顺序的驱动接口）：
  1 申请实验  request_align() / resubmit()  → 写 ExperimentRequest 交给执行方
  2 获取结果  collect()                     → 从适配器取回 ExperimentResult
  3 分析结果  review() 内部 analyze + 策略链 → Assessment → Verdict
  4 决议回写  commit 走守卫式落盘，noop 只合并块

审查中心持有阈值与策略链；会话（CalibrationSession）承载块与延时状态；
执行方（ExecutorAdapter）负责测量并可接收批准的写回计划。三者单向依赖。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from . import writeback
from .contracts import ExperimentRequest, ExperimentResult, Verdict, WritePlan
from .policies import PolicyContext, Thresholds, analyze, default_chain
from .queue_io import ExecutorAdapter
from .session import CalibrationSession
from .solver import SolveResult


@dataclass
class VerdictRecord:
    """一条可回放的裁决留痕。"""
    ts: str
    request_id: str
    kind: str
    targets: list[str]
    action: str
    reason: str
    skew_ns: float | None
    sigma_ns: float | None
    quality: float | None
    written: list[dict] = field(default_factory=list)
    config_hash: str = ""
    attempts: int = 1


class ReviewCenter:
    def __init__(self, session: CalibrationSession, executor: ExecutorAdapter, *,
                 thresholds: Thresholds | None = None, policies: list | None = None,
                 pending_path=None, audit_path="runs/review-audit.jsonl",
                 write: bool = True, params: dict | None = None):
        self.session = session
        self.executor = executor
        self.th = thresholds or Thresholds()
        self.policies = policies or default_chain(self.th)
        self.pending_path = pending_path or session.pending_path
        self.audit_path = audit_path
        self.write = bool(write and self.pending_path)
        self.default_params = dict(params or {})
        self.history: list[VerdictRecord] = []
        self._attempts: dict[str, int] = {}

    # ---------------- 技能 1：申请实验 ----------------

    def request_align(self, new_id: str, ref_id: str, *, into: str | None = None,
                      kind: str = "timing.align", params: dict | None = None,
                      note: str = "") -> ExperimentRequest:
        req = self.session.new_request(kind, new_id, ref_id, into=into,
                                       params={**self.default_params, **(params or {})},
                                       note=note)
        self._attempts[req.id] = 1
        self.executor.submit(req)
        return req

    def resubmit(self, request_id: str) -> ExperimentRequest | None:
        """按原意图重新申请（recheck / retry 分支用；携带最新 config_hash）。"""
        old = self.session.requests.get(request_id)
        if old is None or old.intent.get("op") != "align":
            return None
        it = old.intent
        req = self.session.new_request(old.kind, it["new"], it["ref"], into=it.get("into"),
                                       params=dict(old.params), note=f"重发 ← {request_id}")
        self._attempts[req.id] = self._attempts.get(request_id, 1) + 1
        self.executor.submit(req)
        return req

    # ---------------- 技能 2：获取结果 ----------------

    def collect(self) -> list[ExperimentResult]:
        return self.executor.poll()

    # ---------------- 技能 3 + 4：审查与决议回写 ----------------

    def review(self, result: ExperimentResult) -> Verdict:
        session = self.session
        request = session.requests.get(result.request_id)
        attempts = self._attempts.setdefault(result.request_id, 1)
        assessment = analyze(result, session, request)
        ctx = PolicyContext(session=session, request=request, attempts=attempts,
                            max_attempts=self.th.max_attempts)
        verdict = None
        for policy in self.policies:
            verdict = policy.decide(assessment, ctx)
            if verdict is not None:
                break
        if verdict is None:
            verdict = Verdict("escalate", "策略链未给出裁决", result.request_id)

        applied: list[dict] = []
        if verdict.accepted:
            verdict, applied = self._execute(verdict, assessment.proposed)
        self._record(verdict, result, request, attempts, applied)
        return verdict

    def pump(self) -> list[Verdict]:
        """取回全部可得结果并逐个裁决（编排器每步之后调用一次）。"""
        return [self.review(r) for r in self.collect()]

    # ---------------- 供批量流程一次性调用的写回 ----------------

    def commit_write(self, plan: WritePlan) -> dict:
        """守卫式落盘：CAS + 单调 + 预算 + 原子写（整批一次，而非每条边一次）。"""
        if not self.pending_path:
            raise writeback.WriteGuardError("审查中心未绑定待测文件路径")
        if not self.write:
            raise writeback.WriteGuardError("审查中心处于干跑模式（write=False），拒绝落盘")
        return writeback.commit_shifts(
            self.pending_path, plan.shifts, expected_hash=self.session.config_hash,
            d_max_ns=self.th.d_max_ns, request_id=plan.request_id, reason=plan.reason,
            audit_path=self._audit_for_pending(),
            headroom_return=plan.headroom_return or None,
            granted_headroom=self.session.headroom or None)

    def commit_solve(self, result: SolveResult) -> dict | None:
        """批量求解的唯一提交入口：先验收数值，再决定是否落盘。"""
        result.require_converged()
        if not self.write or not self.session.pending_path:
            return None
        return self.commit_write(result.plan())

    def shift_block(self, block_id: str, delta_ns: float, *, note: str = "ops") -> Verdict:
        """不经实验的人工位移（整块一起右移，如运维调整）：走同一套守卫与留痕。"""
        if delta_ns < 0:
            raise ValueError(f"只允许右移：delta={delta_ns}")
        lines = self.session.blocks[block_id]
        plan = WritePlan(request_id=f"ops-{block_id}", merge=None,
                         shifts={ln: float(delta_ns) for ln in lines}, reason=note)
        verdict, applied = self._execute(
            Verdict("commit", f"块 {block_id} 直接右移 +{delta_ns:g}ns（{note}）",
                    plan.request_id, write_plan=plan), None)
        self.history.append(VerdictRecord(
            ts=datetime.now().isoformat(timespec="seconds"), request_id=plan.request_id,
            kind="ops.shift_block", targets=list(lines), action=verdict.action,
            reason=verdict.reason, skew_ns=None, sigma_ns=None, quality=None,
            written=applied, config_hash=self.session.config_hash, attempts=1))
        return verdict

    # ---------------- 内部：落盘 + 会话推进 ----------------

    def _execute(self, verdict: Verdict, fallback: WritePlan | None):
        """执行已批准的决议：守卫式落盘（commit）→ 会话推进 → 通知执行方。"""
        plan = verdict.write_plan or fallback
        if plan is None:
            return Verdict("replan", "裁决缺少回写计划", verdict.request_id), []
        applied: list[dict] = []
        if verdict.action == "commit" and plan.needs_write:
            if self.write:
                try:
                    receipt = writeback.commit_shifts(
                        self.pending_path, plan.shifts, expected_hash=self.session.config_hash,
                        d_max_ns=self.th.d_max_ns, request_id=plan.request_id,
                        reason=plan.reason, audit_path=self._audit_for_pending(),
                        headroom_return=plan.headroom_return or None,
                        granted_headroom=self.session.headroom or None)
                except writeback.WriteGuardError as exc:
                    return Verdict("recheck", f"回写守卫拒绝：{exc}", verdict.request_id), []
                except PermissionError as exc:
                    # 落盘通道被占用（重试耗尽）：文件保持原样，走 recheck 而不是炸穿流程
                    return Verdict("recheck", f"回写落盘失败（文件占用）：{exc}",
                                   verdict.request_id), []
                applied = receipt.get("applied", [])
            else:
                unknown = [ln for ln in plan.shifts if ln not in self.session.delays]
                if unknown:
                    return Verdict("replan", f"未知线路：{unknown}", verdict.request_id), []
                applied = [{"line": ln, "before": self.session.delays[ln],
                            "delta": round(dl - plan.headroom_return.get(ln, 0.0), 6),
                            "after": round(self.session.delays[ln] + dl
                                           - plan.headroom_return.get(ln, 0.0), 6)}
                           for ln, dl in plan.shifts.items()
                           if dl > 0 or plan.headroom_return.get(ln, 0.0) > 0]
        self.session.apply_plan(plan)
        self.session.refresh_hash()
        self.executor.apply(plan)
        verdict.write_plan = plan
        return verdict, applied

    def _audit_for_pending(self):
        return Path(self.pending_path).with_name("writeback-audit.jsonl") \
            if self.pending_path else "runs/writeback-audit.jsonl"

    def _record(self, verdict: Verdict, result: ExperimentResult,
                request: ExperimentRequest | None, attempts: int,
                applied: list[dict]) -> None:
        self.history.append(VerdictRecord(
            ts=datetime.now().isoformat(timespec="seconds"),
            request_id=result.request_id, kind=result.kind,
            targets=list(request.targets) if request else [],
            action=verdict.action, reason=verdict.reason,
            skew_ns=result.skew_ns, sigma_ns=result.sigma_ns, quality=result.quality,
            written=applied, config_hash=self.session.config_hash, attempts=attempts))

    # ---------------- 视图 ----------------

    def stats(self) -> dict:
        counts = {a: 0 for a in ("commit", "noop", "retry", "recheck", "replan", "escalate")}
        for r in self.history:
            counts[r.action] = counts.get(r.action, 0) + 1
        return {"records": len(self.history), **counts,
                "total_written_ns": round(sum(d.get("delta", 0.0)
                                              for r in self.history for d in r.written), 6)}

    def dump_history(self, path="runs/review-history.json") -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps([asdict(r) for r in self.history], ensure_ascii=False, indent=2)
                     + "\n", encoding="utf-8")
        return p
