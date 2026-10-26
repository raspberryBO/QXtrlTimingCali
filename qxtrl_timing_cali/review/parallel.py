# -*- coding: utf-8 -*-
"""
parallel.py — 并行策略：把一组待测边切成"可以同时开"的波

真实机上校准只是整条实验流程的一环，省时间的主要手段不是少测，而是**同时测**。
本模块只回答一个问题：**哪些边可以放进同一波**（同一波 = 可以一起提交）。

冲突规则（就这一条）：

    两条边冲突 ⇔ 它们共享任意一条线名

注意这里**不**把"共享读取线"算作冲突——同一读取线上的多个腔可以用**多音读取**
分别解调，各比特在读出环节是独立的。真正不能同时开的是"同一条线出现在两个
时序关系里"（例如 `Q0.z↔Q1.z` 与 `CP0001.z↔Q0.z` 都碰 `Q0.z`）。

划分算法：按 (cost, a, b) 排序后做**贪心首次适应**装箱——能塞进现有波就塞，
塞不进（与该波某条边冲突）才开新波。代价小的边先占波，与 `graph.py` 的
最小生成树选边偏好一致。

边界（重要）：

* 波只是**提交提示**，不是并发承诺。执行方完全可以按顺序跑完一波再跑下一波
  （真机上 `hardware_hold` 独占锁时本就如此），结果按 `request_id` 归位，上层无感；
* 跨波的测量仍必须在**同一个 `config_hash` 基线**上完成——写回一旦发生，
  已测的旧观测全部作废（CAS 纪律不因并行而放松）；
* 求解、写回、设备下发**不并行**：并行只影响"观测怎么到达"，
  不影响"一次全局最小二乘 + 一次整批落盘"的语义。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .graph import Pair

SEQUENTIAL = "sequential"        # 一波装全部（max_parallel=1 的退化形态）
WAVES = "conflict_free_waves"    # 按共享线名切分


def shares_line(p: Pair, q: Pair) -> bool:
    """两条边是否共享线名（唯一的冲突判据）。"""
    return bool({p.a, p.b} & {q.a, q.b})


@dataclass
class MeasurementWave:
    """一波：可以一起提交的边。index 从 1 开始（给人看和画动画用）。"""

    index: int
    pairs: list[Pair] = field(default_factory=list)
    why: str = ""

    def accepts(self, pair: Pair) -> bool:
        return not any(shares_line(pair, other) for other in self.pairs)

    def to_dict(self) -> dict:
        return {"index": self.index, "size": len(self.pairs),
                "pairs": [p.to_dict() for p in self.pairs], "why": self.why}


@dataclass
class ParallelPlan:
    """一组边的并行策略：波划分 + 每波最多几条一起提交。"""

    kind: str = WAVES
    waves: list[MeasurementWave] = field(default_factory=list)
    bundle_size: int = 0                 # 0 = 一波就是一个提交束
    rule: str = "同波内的边不共享任何线名（多音读取下各比特独立，共享读取线不构成冲突）"

    @property
    def wave_count(self) -> int:
        return len(self.waves)

    @property
    def edge_count(self) -> int:
        return sum(len(w.pairs) for w in self.waves)

    def bundles(self) -> list[tuple[int, list[Pair]]]:
        """展开成提交束：(波号, 这一束要一起提交的边)。"""
        out: list[tuple[int, list[Pair]]] = []
        for wave in self.waves:
            size = self.bundle_size or len(wave.pairs)
            for i in range(0, len(wave.pairs), max(1, size)):
                out.append((wave.index, wave.pairs[i:i + max(1, size)]))
        return out

    def wave_of(self, pair: Pair) -> int:
        for wave in self.waves:
            if any(p.key == pair.key for p in wave.pairs):
                return wave.index
        return 0

    def to_dict(self) -> dict:
        return {"kind": self.kind, "rule": self.rule, "bundle_size": self.bundle_size,
                "waves": [w.to_dict() for w in self.waves],
                "wave_count": self.wave_count, "edge_count": self.edge_count}


def plan_waves(pairs, *, bundle_size: int = 0) -> ParallelPlan:
    """把一组边切成冲突自由的波。

    bundle_size > 0 时再把每波切成不超过该长度的提交束（执行方并发度有限时用）；
    0 表示"一波就是一个提交束，能开多少由执行方定"。
    """
    pairs = list(pairs)
    if not pairs:
        return ParallelPlan(kind=SEQUENTIAL, waves=[], bundle_size=bundle_size)
    waves: list[MeasurementWave] = []
    for pair in sorted(pairs, key=lambda p: (p.cost, p.a, p.b)):
        fits = [w for w in waves if w.accepts(pair)]
        if fits:
            # 放进"最空的那个可兼容波"，波的大小更均衡 ⇒ 并行度更实
            min(fits, key=lambda w: len(w.pairs)).pairs.append(pair)
        else:
            waves.append(MeasurementWave(
                index=len(waves) + 1, pairs=[pair],
                why=(f"与既有 {len(waves)} 波都冲突（共享线名），另开新波" if waves
                     else "首波：按代价从低到高占位")))
    return ParallelPlan(kind=WAVES, waves=waves, bundle_size=max(0, int(bundle_size)))


def summarize(plan: ParallelPlan) -> str:
    """给人看的一行总结，例如 '3 波 · 21 条边（7+7+7）'。"""
    sizes = "+".join(str(len(w.pairs)) for w in plan.waves) or "0"
    return f"{plan.wave_count} 波 · {plan.edge_count} 条边（{sizes}）"


if __name__ == "__main__":  # 自测：同波内不得共享线名；跨波无所谓
    demo = [Pair(a="Q0.z", b="Q1.z", cost=2.0), Pair(a="CP0001.z", b="Q0.z", cost=3.0),
            Pair(a="Q0.z", b="Q0.xy", cost=1.0), Pair(a="Q2.z", b="Q3.z", cost=2.0)]
    plan = plan_waves(demo)
    for wave in plan.waves:
        print(f"波 {wave.index}: {[p.label for p in wave.pairs]}")
        for i, p in enumerate(wave.pairs):
            for q in wave.pairs[i + 1:]:
                assert not shares_line(p, q), (p, q)
    assert plan.edge_count == len(demo) and len(
        {id(p) for w in plan.waves for p in w.pairs}) == len(demo), "每条边都要且只要出现一次"
    print("OK", summarize(plan))
