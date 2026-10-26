# -*- coding: utf-8 -*-
"""
review — 自动校准审查中心

数据流（批量）：申请(ExperimentRequest) → [外部执行] → 结果(ExperimentResult)
        → 台账留痕(Measurement) → 全局求解(solve) → 写回计划(WritePlan) → 守卫式落盘。

  contracts   五种消息契约（可 JSON 往返）
  session     CalibrationSession：逐线 d 簿记 + 纯计划生成
  policies    analyze（事实）+ quality_problems（质量判据）+ 策略链（阈值裁决）
  center      ReviewCenter：技能 1 申请 / 2 取结果 / 3 审查 / 4 决议回写
  writeback   守卫式回写（CAS + 单调 + 预算 + 原子写 + 审计）
  queue_io    ExecutorAdapter / DirectExecutor / FileQueue
  graph       测量图：候选线对 · 最小生成树选边 · 分量与替补边
  ledger      MeasurementLedger：只记录不写回的测量台账
  solver      加权最小二乘：一堆相对时序 → 逐线右移量
"""
from .center import ReviewCenter, VerdictRecord
from .contracts import (ACTIONS, ADVANCE, Assessment, ExperimentRequest, ExperimentResult,
                        Verdict, WritePlan)
from .graph import (GROUP_COUPLER, GROUP_QUBIT, GROUP_READOUT, GROUP_READOUT_FINAL,
                    STAGE_GROUPS, Pair, bridging_pairs, candidate_pairs, components,
                    minimum_spanning_forest, pairs_of_group)
from .ledger import (JOURNAL_NAME, Measurement, MeasurementLedger, append_journal,
                     journal_path_for, load_journal)
from .policies import (Policy, PolicyContext, Thresholds, analyze, default_chain,
                       quality_problems)
from .queue_io import DirectExecutor, ExecutorAdapter, FileQueue
from .session import CalibrationSession, position_residual
from .solver import RelativeEdge, SolveResult, solve, solve_components
from .writeback import (WriteGuardError, commit_shifts, content_hash, load_pending,
                        read_delays)

__all__ = ["ACTIONS", "ADVANCE", "Assessment", "ExperimentRequest", "ExperimentResult",
           "Verdict", "WritePlan", "CalibrationSession", "position_residual",
           "Thresholds", "PolicyContext", "Policy", "analyze", "default_chain",
           "quality_problems", "ReviewCenter", "VerdictRecord", "WriteGuardError",
           "commit_shifts", "content_hash", "load_pending", "read_delays",
           "ExecutorAdapter", "DirectExecutor", "FileQueue",
           "Pair", "candidate_pairs", "minimum_spanning_forest", "components",
           "bridging_pairs", "pairs_of_group", "Measurement", "MeasurementLedger",
           "JOURNAL_NAME", "load_journal", "append_journal", "journal_path_for",
           "STAGE_GROUPS", "GROUP_READOUT", "GROUP_QUBIT", "GROUP_COUPLER",
           "GROUP_READOUT_FINAL",
           "RelativeEdge", "SolveResult", "solve", "solve_components"]
