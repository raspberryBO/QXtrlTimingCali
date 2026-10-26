# -*- coding: utf-8 -*-
"""
sim.py — 模拟设备与参考执行方（真实测控软件的替身）

  SimChip      —— 设备替身：持有隐藏真值 L（来自 truth 文件）与已施加右移 d，
                  只提供位置 / 测量 / 施加位移三件事，不含任何审查或编排逻辑；
  SimExecutor  —— 参考执行方：按 ExperimentRequest 测量两线 skew，返回 ExperimentResult
                  （n_shots / repeats / noise_ns 决定 sigma 与 quality），并接收批准的
                  WritePlan 作用到设备上。

独立性：本模块只依赖 review.contracts 的数据类（不 import 会话 / 策略 / 中心），
将来整体替换为真实设备适配器时接口不变。
"""
from __future__ import annotations

import json
import math
import random
import statistics
from datetime import datetime
from pathlib import Path

from .review.contracts import ExperimentRequest, ExperimentResult, WritePlan


class SimChip:
    """设备时序替身：q(line) = L + d，d 只增不减。"""

    def __init__(self, latencies: dict[str, float], *, noise_ns: float = 0.0,
                 seed: int | None = None):
        self._l = {k: float(v) for k, v in latencies.items()}
        self._d = {k: 0.0 for k in self._l}
        self.noise_ns = float(noise_ns)
        self._rng = random.Random(seed)

    def lines(self) -> list[str]:
        return list(self._l)

    def truth(self, line: str) -> float:
        return self._l[line]

    def applied_shift(self, line: str) -> float:
        return self._d[line]

    def position(self, line: str) -> float:
        return self._l[line] + self._d[line]

    def apply_shift(self, line: str, delta_ns: float) -> None:
        if line not in self._d:
            raise KeyError(f"未知线路：{line}")
        if delta_ns < 0:
            raise ValueError(f"只允许右移：{line} delta={delta_ns}")
        self._d[line] = round(self._d[line] + float(delta_ns), 6)

    def apply_delta(self, line: str, delta_ns: float) -> None:
        """净位移下发（可负）：收回 headroom 时设备跟随左移。

        只允许右移的守卫在上游（会话 + 回写守卫链）已经校验过收回量不超过
        授予余额，这里直接记账。
        """
        if line not in self._d:
            raise KeyError(f"未知线路：{line}")
        self._d[line] = round(self._d[line] + float(delta_ns), 6)

    def measure_skew(self, a: str, b: str) -> float:
        """一次相对时序测量：返回 q_a − q_b（无噪声时精确）。"""
        v = self.position(a) - self.position(b)
        return v + self._rng.gauss(0.0, self.noise_ns) if self.noise_ns else v


class SimExecutor:
    """参考执行方：run(request) → ExperimentResult；apply_plan(plan) → 下发到设备。"""

    def __init__(self, chip: SimChip, *, noise_ns: float | None = None, seed: int | None = None,
                 time_fmt: str = "%Y-%m-%dT%H:%M:%S", force: dict | None = None):
        self.chip = chip
        self.noise_ns = chip.noise_ns if noise_ns is None else float(noise_ns)
        self._rng = random.Random(seed)
        self.time_fmt = time_fmt
        self.requests: list[ExperimentRequest] = []
        self.force = dict(force or {})      # 测试注入：{"flags":[...],"quality":0.1,...}

    # ---- 执行方接口（对应 review.queue_io.ExecutorAdapter 约定）----

    def run(self, request: ExperimentRequest) -> ExperimentResult:
        self.requests.append(request)
        new_line, ref_line = request.targets
        params = {**self.force, **request.params}
        n_shots = int(params.get("n_shots", 1000))
        repeats = max(1, int(params.get("repeats", 1)))
        sigma_single = float(params.get("noise_ns", self.noise_ns))

        groups = []
        for _ in range(repeats):
            samples = [self.chip.measure_skew(new_line, ref_line) for _ in range(n_shots)]
            groups.append(statistics.fmean(samples))
        skew = statistics.fmean(groups)
        if repeats > 1 and n_shots > 1:
            sigma = statistics.stdev(groups) / math.sqrt(repeats)
        else:
            sigma = sigma_single / math.sqrt(max(1, n_shots * repeats))
        ref = float(params.get("quality_ref_ns", 1.0))
        quality = params.get("quality")
        if quality is None:
            quality = math.exp(-(sigma / ref) ** 2) if ref > 0 else 1.0

        return ExperimentResult(
            request_id=request.id, kind=request.kind,
            config_hash=params.get("config_hash", request.config_hash),
            measured_at=datetime.now().strftime(self.time_fmt),
            skew_ns=round(skew, 6), sigma_ns=round(sigma, 6), quality=round(float(quality), 6),
            flags=list(params.get("flags", [])), n_shots=n_shots * repeats,
            repeats=[{"skew_ns": round(g, 6)} for g in groups] if repeats > 1 else [],
            artifacts={"source": "sim", "new": new_line, "ref": ref_line})

    def apply_plan(self, plan: WritePlan) -> None:
        """把审查批准并落盘的写回同样施加到设备上（保持模拟与配置一致）。

        逐线净位移 = shifts − headroom_return（收回 headroom 允许设备左移；
        守卫链已保证收回量不超过授予余额，这里不再重复校验）。
        先全量校验线路存在性再下发：中途抛 KeyError 会把设备留在半状态。
        """
        net: dict[str, float] = {}
        for line, delta in plan.shifts.items():
            net[line] = net.get(line, 0.0) + float(delta)
        for line, ret in plan.headroom_return.items():
            net[line] = net.get(line, 0.0) - float(ret)
        known = set(self.chip.lines())
        unknown = sorted(ln for ln in net if ln not in known)
        if unknown:
            raise KeyError(f"设备上不存在线路：{unknown}（一个位移都没下发）")
        for line, delta in net.items():
            self.chip.apply_delta(line, delta)


def chip_from_truth(truth_path_or_dict) -> SimChip:
    """由 truth 文件（line + delay = 初始位置 L）构造设备替身。"""
    data = truth_path_or_dict
    if not isinstance(data, dict):
        data = json.loads(Path(data).read_text(encoding="utf-8"))
    return SimChip({e["line"]: float(e["delay"]) for e in data["lines"]})


def seed_applied_shifts(device: SimChip, delays: dict[str, float]) -> int:
    """按待测文件当前的 d 还原设备已施加位移，返回还原的条数。

    分批驱动 / 续跑时**一个进程一份设备**：新进程的 SimChip 从 q = L 起算，
    而前几组的位移早已写进 pending——不还原的话，这一进程测到的是另一个世界。
    设备上不存在的线名静默跳过（没给 truth 时无从装载）。
    """
    known = set(device.lines())
    n = 0
    for ln, d in delays.items():
        if d > 0 and ln in known:
            device.apply_shift(ln, d)
            n += 1
    return n
