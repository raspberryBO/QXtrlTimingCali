# -*- coding: utf-8 -*-
"""
graph.py — 测量图：哪些线对可测，以及怎么挑出"最少且够用"的一组

批量校准的第一件事是先决定**测什么**。原则是：

  * 一条边 = 一次相对时序实验，测得 skew = q(a) − q(b)（a=new、b=ref）；
  * N 条线只需要 N−1 条边就能定出全部相对关系——正如 4 个比特围成方块有 4 条物理边，
    测 3 条就足以推断第 4 条，多测一条都是浪费实验时间；
  * 于是按**代价最小生成树**挑边：优先便宜的本地边（同比特 xy↔z、同读取线 ri↔ro），
    跨比特 / 跨类型的边更贵，只在必须搭桥时才用。

代价可用 cost 钩子改写（比如真机上 xy-ro 实验特别慢）。
某条边彻底测不出来时它会把图劈成两半，`bridging_pairs()` 负责找替补边重新搭桥，
实在接不上的线由上层当作**隔离区**处理（不写回、列进报告）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

# 实验分组：实际校准必须按这个顺序做，前一组是后一组的前提
GROUP_READOUT = "readout"          # 读取线：ro/ri 对齐（其余实验都靠它读出信号）
GROUP_QUBIT = "qubit"              # 单比特 XY-Z：比特内 xy ↔ z
GROUP_COUPLER = "coupler"          # 比特间 z-z / 耦合器 z 挂到相邻比特
GROUP_READOUT_FINAL = "readout_final"  # 读取线最终挂到已对齐的 qubit.xy
STAGE_GROUPS = (GROUP_READOUT, GROUP_QUBIT, GROUP_COUPLER, GROUP_READOUT_FINAL)

# 默认代价（越小越优先被生成树选中；单位"次实验的相对开销"）
COST_BIT_LOCAL = 1.0       # Q.xy ↔ Q.z：同一比特内部
COST_LINE_LOCAL = 1.0      # RL.ro ↔ RL.ri：同一读取线内部
COST_QUBIT_QUBIT = 2.0     # Qa.z ↔ Qb.z：跨比特 z-z（经耦合器）
COST_COUPLER = 3.0         # CP.z ↔ Q.z（可调）或 CP.z ↔ Q.xy（不可调）
COST_READOUT_FINAL = 2.0   # RL.ro/ri ↔ Q.xy：读取线最终对齐到 qubit


@dataclass(frozen=True)
class Pair:
    """一条待测量线对。a=new、b=ref，约定测得 skew_ns = q(a) − q(b)。

    group 标记它属于哪一组实验（见 STAGE_GROUPS）：同组才能在同一阶段一起测、
    一起解，跨组的观测不能混在同一批求解里（它们的测量基线不同）。
    """

    a: str
    b: str
    kind: str = "timing.align"
    cost: float = 1.0
    note: str = ""
    group: str = GROUP_QUBIT

    def __post_init__(self) -> None:
        if self.a == self.b:
            raise ValueError(f"测量边不能自环：{self.a}")
        if self.cost <= 0:
            raise ValueError(f"测量边代价须为正：{self.cost}")
        if self.group not in STAGE_GROUPS:
            raise ValueError(f"未知实验分组：{self.group}（应为 {STAGE_GROUPS}）")

    @property
    def key(self) -> frozenset[str]:
        return frozenset((self.a, self.b))

    @property
    def label(self) -> str:
        return self.note or f"{self.a} ↔ {self.b}"

    def to_dict(self) -> dict:
        """序列化只保留与测量有关的字段；`group` 由分组流程自己附加（见 stages）。"""
        return {"a": self.a, "b": self.b, "kind": self.kind,
                "cost": self.cost, "note": self.note}


# ===================== 候选边 =====================

def candidate_pairs(chip: dict, lines: Iterable[str] | None = None,
                    cost: Callable[[str, str, float], float] | None = None) -> list[Pair]:
    """由芯片结构推导全部**物理上可测**的线对。

    lines 给出时只保留两端都存在的边（不可调芯片没有 z 线，会自动滤掉）。
    cost(a, b, default_cost) 可覆写代价，用于表达真机上的实验开销差异。
    """
    known = set(lines) if lines is not None else None
    out: list[Pair] = []
    seen: set[frozenset[str]] = set()

    def add(a: str, b: str, default: float, note: str, group: str,
            kind: str = "timing.align") -> None:
        if known is not None and (a not in known or b not in known):
            return
        pair = Pair(a=a, b=b, kind=kind, group=group,
                    cost=float(cost(a, b, default) if cost else default), note=note)
        if pair.key in seen:
            return
        seen.add(pair.key)
        out.append(pair)

    # 0 辅助映射
    qubits_with_z = {f"{q['id']}.z" for q in chip["qubits"]} if known is None else {
        f"{q['id']}.z" for q in chip["qubits"] if f"{q['id']}.z" in known
    }

    # 1 同一比特内部：xy ↔ z —— 单比特实验的前提（X_π 要能对准 z 门）
    for q in chip["qubits"]:
        qid = q["id"]
        if f"{qid}.z" in qubits_with_z:
            add(f"{qid}.z", f"{qid}.xy", COST_BIT_LOCAL, f"{qid} xy-z 对齐", GROUP_QUBIT)
    # 2 同一读取线内部：ri ↔ ro —— 一切实验的读出前提，必须最先对齐
    for line in chip["readout_lines"]:
        lid = line["id"]
        add(f"{lid}.ri", f"{lid}.ro", COST_LINE_LOCAL, f"{lid} 读取对 ri-ro", GROUP_READOUT)
    # 3 耦合器阶段：相邻比特间 z-z + 耦合器 z 挂到相邻比特
    #    可调比特用 Q.z；不可调比特退化为 Q.xy 作为 CP.z 的锚点。
    for cp in chip["couplers"]:
        qa, qb = cp["qubits"]
        if f"{qa}.z" in qubits_with_z and f"{qb}.z" in qubits_with_z:
            add(f"{qa}.z", f"{qb}.z", COST_QUBIT_QUBIT, f"{qa}↔{qb} z-z", GROUP_COUPLER)
        cpid = cp["id"]
        for qid in (qa, qb):
            if f"{qid}.z" in qubits_with_z:
                add(f"{cpid}.z", f"{qid}.z", COST_COUPLER, f"{cpid} z 挂 {qid}.z", GROUP_COUPLER)
            else:
                add(f"{cpid}.z", f"{qid}.xy", COST_COUPLER, f"{cpid} z 挂 {qid}.xy", GROUP_COUPLER)
    # 4 读取线最终对齐：每条读取线选第一个腔上的 qubit.xy，把读出链挂过去。
    #    这一步发生在 qubit/coupler 全部对齐并写回之后，因此以 Q.xy 为锚。
    #    步 1 已把 ro/ri 对齐，此后二者同步增减，所以这里每线只测**一条**边
    #    （代表线取 ro；ri 的位移由 stages 按同量补上，不单独占一次实验）。
    first_qubit_of_line: dict[str, str] = {}
    for c in chip["cavities"]:
        lid = c["line"]
        if lid not in first_qubit_of_line:
            first_qubit_of_line[lid] = c["qubit"]
    for lid, qid in first_qubit_of_line.items():
        # a=readout, b=qubit.xy：skew = q(readout) - q(qubit)，用于 final 阶段把 readout 拉过去。
        add(f"{lid}.ro", f"{qid}.xy", COST_READOUT_FINAL, f"{lid} ro 挂 {qid}", GROUP_READOUT_FINAL)

    return sorted(out, key=lambda p: (p.cost, p.a, p.b))


def pairs_of_group(candidates: Iterable[Pair], group: str) -> list[Pair]:
    """挑出某一组实验的候选边（组间基线不同，绝不能混着测、混着解）。"""
    return [p for p in candidates if p.group == group]


# ===================== 最小生成森林 =====================

def _find(parent: dict[str, str], x: str) -> str:
    while parent[x] != x:
        parent[x] = parent[parent[x]]
        x = parent[x]
    return x


def minimum_spanning_forest(lines: Iterable[str], candidates: Iterable[Pair]) -> list[Pair]:
    """Kruskal：每条线配到各自分量所需的最少一组边（连通时恰为 N−1 条）。"""
    nodes = list(dict.fromkeys(lines))
    known = set(nodes)
    parent = {ln: ln for ln in nodes}
    chosen: list[Pair] = []

    def union(a: str, b: str) -> bool:
        ra, rb = _find(parent, a), _find(parent, b)
        if ra == rb:
            return False
        parent[rb] = ra
        return True

    for pair in sorted(candidates, key=lambda p: (p.cost, p.a, p.b)):
        if pair.a not in known or pair.b not in known:
            continue
        if union(pair.a, pair.b):
            chosen.append(pair)
    return chosen


# ===================== 连通性与替补边 =====================

def components(lines: Iterable[str], edges: Sequence[Pair | tuple[str, str]]) -> list[list[str]]:
    """按已成功的边划分连通分量；孤立线各自成一分量。返回各分量内已排序。"""
    nodes = list(dict.fromkeys(lines))
    parent = {ln: ln for ln in nodes}

    def union(a: str, b: str) -> None:
        ra, rb = _find(parent, a), _find(parent, b)
        if ra != rb:
            parent[rb] = ra

    for e in edges:
        a, b = (e.a, e.b) if isinstance(e, Pair) else (e[0], e[1])
        if a in parent and b in parent:
            union(a, b)

    groups: dict[str, list[str]] = {}
    for ln in nodes:
        groups.setdefault(_find(parent, ln), []).append(ln)
    return [sorted(g) for g in groups.values()]


def _key(item) -> frozenset[str]:
    """取一条边两端的集合；既能吃 Pair，也能吃带 .pair 的记录对象。"""
    pair = getattr(item, "pair", item)
    return frozenset((pair.a, pair.b))


def bridging_pairs(lines: Iterable[str], comps: Sequence[Sequence[str]],
                   candidates: Iterable[Pair], used: Iterable[Pair] | None = None) -> list[Pair]:
    """能把两个不同分量重新连起来的候选边，按代价升序（排除已试过的）。"""
    known = set(lines)
    index_of: dict[str, int] = {}
    for i, comp in enumerate(comps):
        for ln in comp:
            index_of[ln] = i
    tried = {_key(p) for p in (used or ())}

    out = []
    for p in candidates:
        if p.a not in known or p.b not in known or p.key in tried:
            continue
        ia, ib = index_of.get(p.a), index_of.get(p.b)
        if ia is None or ib is None or ia == ib:
            continue
        out.append(p)
    return sorted(out, key=lambda p: (p.cost, p.a, p.b))


def _demo() -> None:
    """自测：4 比特方格（4 条耦合边）只需 3 条测量边。"""
    lines = ["Q0.xy", "Q1.xy", "Q2.xy", "Q3.xy"]
    cands = [Pair(a=lines[i], b=lines[j], cost=1.0) for i in range(4) for j in range(i + 1, 4)]
    edges = minimum_spanning_forest(lines, cands[:4])        # 只给构成方格的 4 条边
    assert len(edges) == 3, edges
    assert len(components(lines, edges)) == 1
    print(f"4 比特 / 4 条候选边 → 生成树 {len(edges)} 条：{[e.label for e in edges]}")
    print("missing:", components(lines, []) and "每条线一个孤立分量")


if __name__ == "__main__":
    _demo()
