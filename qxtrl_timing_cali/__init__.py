# -*- coding: utf-8 -*-
"""QXtrlTimingCali — 超导量子比特 Timing 校准系统

零第三方依赖（Python 标准库）。子模块惰性导出，保持 `python -m qxtrl_timing_cali.<mod>` 干净可跑。

  模拟端：chip_gen（芯片文件）· wiring_gen（truth/pending 延时文件）· validate · visualize
  审查端：review.*（契约 / 会话 / 策略 / 测量图 / 台账 / 求解 / 回写 / 队列适配）
  替身：sim（设备替身）
  批量校准：pipeline —— 选最少测量集 → 只测量 → 全局求解 → 整批一次写回
  分组校准：stages —— 四组六步按真实实验顺序编排：读取线 ro↔ri 对齐 + headroom 授予（即时写回）→ 单比特 XY-Z → 比特间/耦合器 Z-Z（后两组只记录）→ 末尾统一求解、整批一次写回 → 读取线收回 headroom、拉回对齐（即时写回）
"""

__version__ = "0.4.1"

_EXPORTS = {
    "generate_chip": "chip_gen", "line_names": "chip_gen", "save_chip": "chip_gen",
    "generate_wiring": "wiring_gen", "save_wiring": "wiring_gen",
    "validate_chip": "validate", "validate_wiring": "validate",
    "render_chip_html": "visualize", "save_visualization": "visualize",
    "ReviewCenter": "review", "CalibrationSession": "review", "Thresholds": "review",
    "ExperimentRequest": "review", "ExperimentResult": "review", "Verdict": "review",
    "WritePlan": "review", "Assessment": "review", "FileQueue": "review",
    "DirectExecutor": "review", "commit_shifts": "review", "position_residual": "review",
    "analyze": "review", "quality_problems": "review",
    "Pair": "review", "candidate_pairs": "review", "minimum_spanning_forest": "review",
    "components": "review", "bridging_pairs": "review", "pairs_of_group": "review",
    "STAGE_GROUPS": "review", "GROUP_READOUT": "review", "GROUP_QUBIT": "review",
    "GROUP_COUPLER": "review", "GROUP_READOUT_FINAL": "review",
    "load_journal": "review", "append_journal": "review", "JOURNAL_NAME": "review",
    "journal_path_for": "review",
    "Measurement": "review", "MeasurementLedger": "review",
    "RelativeEdge": "review", "SolveResult": "review", "solve": "review",
    "solve_components": "review",
    "SimChip": "sim", "SimExecutor": "sim",
    "run_pipeline": "pipeline", "CalibrationPipeline": "pipeline",
    "plan_stages": "stages", "StagePlan": "stages", "StageOutcome": "stages",
    "StagedCalibrationPipeline": "stages", "run_staged_pipeline": "stages",
}

__all__ = list(_EXPORTS) + ["__version__"]


def __getattr__(name):
    mod = _EXPORTS.get(name)
    if mod is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module
    return getattr(import_module(f".{mod}", __name__), name)
