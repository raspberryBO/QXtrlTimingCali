# -*- coding: utf-8 -*-
"""
validate.py — 芯片 / 连线配置文件校验器（v1.0 规则，标准库实现）

对外接口（两种入参形态均可）：
  validate_chip(path_or_dict)                 -> (errors, warnings)
  validate_wiring(path_or_dict, chip=None)    -> (errors, warnings)
    chip 可传芯片 dict 或文件路径，用于线路集合交叉校验。

校验分组：
  芯片：1 结构 / 2 命名唯一 / 3 引用一致（1:1 与双向）/ 4 数值范围 / 5 连接图 / 附 范围与标定值
  连线：1 结构（含 meta.kind: truth|pending）/ 2 唯一（线路名；通道可选）
        / 3 数值（delay ≥ 0 只右移）/ 4 交叉（可选）
        channel 为可选字段：模拟连线文件只含 line + delay。
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

REQUIRED_CHIP_SECTIONS = ("schema_version", "meta", "units", "qubits", "cavities",
                          "couplers", "readout_lines", "connections")


def _load(obj, label: str) -> tuple[dict | None, list[str]]:
    if isinstance(obj, dict):
        return obj, []
    try:
        data = json.loads(Path(obj).read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return None, [f"[{label}] JSON 解析失败：{exc}"]
    if not isinstance(data, dict):
        return None, [f"[{label}] 文件内容不是对象（dict）"]
    return data, []


def _items(data: dict, sec: str, errs: list[str]) -> list[dict]:
    """取一个对象数组的节，坏条目记错误后剔除——校验器的本职是处理坏文件，
    绝不能因为下标访问坏数据把已收集的错误信息一起带崩。"""
    v = data.get(sec)
    if not isinstance(v, list):
        errs.append(f"[结构] {sec} 节应为对象数组（当前 {type(v).__name__}）")
        return []
    out: list[dict] = []
    for i, item in enumerate(v):
        if not isinstance(item, dict):
            errs.append(f"[结构] {sec}[{i}] 应为对象（当前 {type(item).__name__}）")
            continue
        if not isinstance(item.get("id"), str) or not item["id"]:
            errs.append(f"[结构] {sec}[{i}] 缺少非空字符串 id")
            continue
        out.append(item)
    return out


# ============================ 芯片文件 ============================

def validate_chip(path_or_dict) -> tuple[list[str], list[str]]:
    data, errs = _load(path_or_dict, "芯片")
    if data is None:
        return errs, []
    warnings: list[str] = []

    # 1 结构
    for sec in REQUIRED_CHIP_SECTIONS:
        if sec not in data:
            errs.append(f"[结构] 缺少必需节：{sec}")
    if data.get("schema_version") != "1.0":
        errs.append(f"[结构] schema_version 应为 \"1.0\"（当前 {data.get('schema_version')!r}）")
    if not (isinstance(data.get("meta"), dict) and data["meta"].get("id")):
        errs.append("[结构] meta.id 缺失")
    if not isinstance((data.get("meta") or {}).get("tunable"), bool):
        errs.append("[结构] meta.tunable 缺失或不是布尔值（整片可调性）")
    if errs:
        return errs, warnings

    qubits = _items(data, "qubits", errs)
    cavities = _items(data, "cavities", errs)
    couplers = _items(data, "couplers", errs)
    lines = _items(data, "readout_lines", errs)
    if errs:
        return errs, warnings

    # 2 命名唯一
    ids = ([q["id"] for q in qubits] + [c["id"] for c in cavities]
           + [cp["id"] for cp in couplers] + [l["id"] for l in lines])
    dup_ids = sorted({x for x in ids if ids.count(x) > 1})
    if dup_ids:
        errs.append(f"[唯一] 元素 id 重复：{dup_ids}")

    # 3 引用一致
    qids = {q["id"] for q in qubits}
    cids = {c["id"] for c in cavities}
    lids = {l["id"] for l in lines}
    q_by_id = {q["id"]: q for q in qubits}
    c_by_id = {c["id"]: c for c in cavities}
    for q in qubits:
        if q.get("cavity") not in cids:
            errs.append(f"[引用] 比特 {q['id']} 的腔 {q.get('cavity')!r} 不存在")
        else:
            cav = c_by_id[q["cavity"]]
            if cav.get("qubit") != q["id"]:
                errs.append(f"[引用] {q['id']}.cavity={q['cavity']} 与 "
                            f"{q['cavity']}.qubit={cav.get('qubit')!r} 不一致（要求 1:1）")
    for c in cavities:
        if c.get("qubit") not in qids:
            errs.append(f"[引用] 腔 {c['id']} 的比特 {c.get('qubit')!r} 不存在")
        if c.get("line") not in lids:
            errs.append(f"[引用] 腔 {c['id']} 的读取线 {c.get('line')!r} 不存在")
    for l in lines:
        cav_list = l.get("cavities")
        if not isinstance(cav_list, list):
            errs.append(f"[引用] 读取线 {l['id']} 的 cavities 应为列表")
            cav_list = []
        for cc in cav_list:
            if cc not in cids:
                errs.append(f"[引用] 读取线 {l['id']} 引用了不存在的腔 {cc}")
            elif c_by_id[cc].get("line") != l["id"]:
                errs.append(f"[引用] {cc}.line={c_by_id[cc].get('line')!r} 与"
                            f"读取线 {l['id']}.cavities 不一致")
    coupler_by_id = {cp["id"]: cp for cp in couplers}
    for cp in couplers:
        qs = cp.get("qubits")
        if not isinstance(qs, list) or len(qs) != 2:
            errs.append(f"[引用] 耦合器 {cp['id']} 的 qubits 应为两项")
            continue
        for qq in qs:
            if qq not in qids:
                errs.append(f"[引用] 耦合器 {cp['id']} 引用了不存在的比特 {qq}")

    for q in qubits:
        matches = [c["id"] for c in cavities if c.get("qubit") == q["id"]]
        if len(matches) == 0:
            errs.append(f"[引用] 比特 {q['id']} 未挂载到任何腔（1:1 要求）")
        elif len(matches) > 1:
            errs.append(f"[引用] 比特 {q['id']} 同时挂载在多个腔：{matches}（要求 1:1）")
    for c in cavities:
        matches = [q["id"] for q in qubits if q.get("cavity") == c["id"]]
        if len(matches) == 0:
            errs.append(f"[引用] 腔 {c['id']} 未挂载比特（1:1 要求）")
        elif len(matches) > 1:
            errs.append(f"[引用] 腔 {c['id']} 挂载了多个比特：{matches}（要求 1:1）")

    conn = data.get("connections")
    if not isinstance(conn, list):
        errs.append(f"[连接] connections 应为对象数组（当前 {type(conn).__name__}）")
        conn = []
    got_qc, got_cl, got_via = set(), set(), set()
    for row in conn:
        if not isinstance(row, dict):
            errs.append("[连接] connections 项应为对象")
            continue
        t = row.get("type")
        if t == "qubit-cavity":
            qq, cc = row.get("qubit"), row.get("cavity")
            got_qc.add((qq, cc))
            q_obj = q_by_id.get(qq)
            if q_obj is None or q_obj.get("cavity") != cc:
                errs.append(f"[连接] connections 项 {qq}↔{cc} 与比特 cavity 字段不一致")
        elif t == "cavity-line":
            cc, ll = row.get("cavity"), row.get("line")
            got_cl.add((cc, ll))
            c_obj = c_by_id.get(cc)
            if c_obj is None or c_obj.get("line") != ll:
                errs.append(f"[连接] connections 项 {cc}↔{ll} 与腔 line 字段不一致")
        elif t == "qubit-qubit":
            via = row.get("via")
            got_via.add(via)
            cp = coupler_by_id.get(via)
            if cp is None:
                errs.append(f"[连接] 引用了不存在的耦合器 {via}")
            elif set(cp.get("qubits") or ()) != {row.get("u"), row.get("v")}:
                errs.append(f"[连接] {via} 的 qubits 与 connections 项不一致")
        else:
            warnings.append(f"[连接] 未知连接类型：{t!r}")
    derived_qc = {(q["id"], q.get("cavity")) for q in qubits if q.get("cavity") is not None}
    if gone := derived_qc - got_qc:
        errs.append(f"[连接] connections 缺少 qubit-cavity 项：{sorted(gone)}")
    if extra := got_qc - derived_qc:
        errs.append(f"[连接] connections 多出 qubit-cavity 项：{sorted(extra)}")
    derived_cl = {(c["id"], c.get("line")) for c in cavities if c.get("line") is not None}
    if gone := derived_cl - got_cl:
        errs.append(f"[连接] connections 缺少 cavity-line 项：{sorted(gone)}")
    if extra := got_cl - derived_cl:
        errs.append(f"[连接] connections 多出 cavity-line 项：{sorted(extra)}")
    derived_via = set(coupler_by_id)
    if gone := derived_via - got_via:
        errs.append(f"[连接] connections 缺少 qubit-qubit 项：{sorted(gone)}")
    if extra := got_via - derived_via:
        errs.append(f"[连接] connections 多出 qubit-qubit 项：{sorted(extra)}")

    # 4 数值范围
    for q in qubits:
        d = q.get("design")
        if d is None:
            warnings.append(f"[数值] 比特 {q['id']} 缺少 design 参数块")
            continue
        f01 = d.get("f01")
        if f01 is not None and not (3.0 < f01 < 10.0):
            warnings.append(f"[数值] {q['id']}.f01={f01} 超出常见范围 3–10 GHz")
        a = d.get("anharmonicity")
        if a is not None and a > 0:
            warnings.append(f"[数值] {q['id']}.anharmonicity 通常为负值（当前 {a}）")
        g = d.get("g_cavity")
        if g is not None and g < 0:
            errs.append(f"[数值] {q['id']}.g_cavity 不能为负")
    for c in cavities:
        d = c.get("design") or {}
        k = d.get("kappa")
        if k is not None and k <= 0:
            errs.append(f"[数值] 腔 {c['id']}.kappa 应为正")
        fr = d.get("f_r")
        if fr is not None and not (3.0 < fr < 12.0):
            warnings.append(f"[数值] 腔 {c['id']}.f_r={fr} 超出常见范围 3–12 GHz")
    for cp in couplers:
        d = cp.get("design") or {}
        gmax = d.get("g_max")
        if gmax is not None and gmax < 0:
            errs.append(f"[数值] 耦合器 {cp['id']}.g_max 不能为负")

    # 5 连接图
    adj: dict[str, set] = {q: set() for q in qids}
    pair_count: dict[frozenset, int] = {}
    for cp in couplers:
        qs = cp.get("qubits")
        if not isinstance(qs, list) or len(qs) != 2:
            continue                          # 引用一致一节已记错
        a, b = qs
        if a == b:
            errs.append(f"[图] 耦合器 {cp['id']} 两端为同一比特")
            continue
        if a not in adj or b not in adj:
            continue                          # 引用了不存在的比特：引用一节已记错
        adj[a].add(b)
        adj[b].add(a)
        key = frozenset({a, b})
        pair_count[key] = pair_count.get(key, 0) + 1
    for key, n in pair_count.items():
        if n > 1:
            warnings.append(f"[图] 两比特 {sorted(key)} 之间存在多个耦合器")
    isolated = sorted(q for q, nb in adj.items() if not nb)
    if isolated:
        warnings.append(f"[图] 比特无耦合器连接：{isolated}")
    if qids:
        start = next(iter(qids))
        seen, stack = {start}, [start]
        while stack:
            u = stack.pop()
            for v in adj[u]:
                if v not in seen:
                    seen.add(v)
                    stack.append(v)
        unreached = sorted(qids - seen)
        if unreached:
            warnings.append(f"[图] 耦合图不连通，未覆盖：{unreached}")

    # 附 1：范围检查；附 2：标定值不应写入本文件
    if "wiring" in data:
        warnings.append("[范围] 出现 wiring 节：外接线信息不属于芯片文件（请移入连线文件）")
    if "calibration" in data:
        warnings.append("[范围] 出现 calibration 节：校准参数属于连线文件（请移出）")
    for q in qubits:
        if "channels" in q:
            warnings.append(f"[范围] 比特 {q['id']} 出现 channels 字段（线名由约定推导）")
        if "type" in q:
            warnings.append(f"[范围] 比特 {q['id']} 出现 type 字段（可调性由 meta.tunable 声明）")
    for cp in couplers:
        if "channels" in cp:
            warnings.append(f"[范围] 耦合器 {cp['id']} 出现 channels 字段（线名由约定推导）")
        if "type" in cp:
            warnings.append(f"[范围] 耦合器 {cp['id']} 出现 type 字段（可调性由 meta.tunable 声明）")
    for l in lines:
        if "channels" in l:
            warnings.append(f"[范围] 读取线 {l['id']} 出现 channels 字段（线名由约定推导）")
    if any("delay" in str(k) for coll in (qubits, cavities, couplers, lines)
           for item in coll for k in (item or {})):
        warnings.append("[标定] 芯片文件字段出现 delay 字样：标定值请存连线文件")

    return errs, warnings


# ============================ 连线文件 ============================

def validate_wiring(path_or_dict, chip=None) -> tuple[list[str], list[str]]:
    data, errs = _load(path_or_dict, "连线")
    if data is None:
        return errs, []
    warnings: list[str] = []

    # 1 结构
    for sec in ("schema_version", "meta", "lines"):
        if sec not in data:
            errs.append(f"[结构] 缺少必需节：{sec}")
    if data.get("schema_version") != "1.0":
        errs.append(f"[结构] schema_version 应为 \"1.0\"（当前 {data.get('schema_version')!r}）")
    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    if not meta.get("id"):
        errs.append("[结构] meta.id 缺失")
    if not meta.get("chip"):
        errs.append("[结构] meta.chip 缺失（应指向芯片 id）")
    kind = meta.get("kind")
    if kind is None:
        warnings.append("[结构] meta.kind 缺失：应声明 truth（真实值 L）或 pending（待测 d）")
    elif kind not in ("truth", "pending"):
        errs.append(f"[结构] meta.kind 应为 truth|pending（当前 {kind!r}）")
    if errs:
        return errs, warnings

    raw_lines = data.get("lines")
    if not isinstance(raw_lines, list):
        errs.append(f"[结构] lines 节应为对象数组（当前 {type(raw_lines).__name__}）")
        raw_lines = []
    lines = [e for e in raw_lines if isinstance(e, dict)]
    if len(lines) != len(raw_lines):
        errs.append(f"[结构] lines 含非对象条目（{len(raw_lines) - len(lines)} 个已忽略）")

    # 2 唯一
    names = [e.get("line") for e in lines]
    dupn = sorted({x for x in names if names.count(x) > 1})
    if dupn:
        errs.append(f"[唯一] 线路名重复：{dupn}")
    chans = [e["channel"] for e in lines if isinstance(e.get("channel"), str)]
    dupc = sorted({x for x in chans if chans.count(x) > 1})
    if dupc:
        errs.append(f"[唯一] 通道标识重复：{dupc}")

    # 3 数值 / 类型
    for e in lines:
        d = e.get("delay")
        if not isinstance(d, (int, float)) or isinstance(d, bool):
            errs.append(f"[数值] 线路 {e.get('line')} 的 delay 缺失或非数字")
        elif not math.isfinite(d):
            errs.append(f"[数值] 线路 {e.get('line')} 的 delay={d} 不是有限数值")
        elif d < 0:
            errs.append(f"[数值] 线路 {e.get('line')} 的 delay={d} 为负（只右移，应 ≥ 0）")
        ch = e.get("channel")
        if ch is not None and (not isinstance(ch, str) or not ch):
            errs.append(f"[数值] 线路 {e.get('line')} 的 channel 应为非空字符串")

    # 4 交叉校验（可选）
    if chip is not None:
        chip_data, cerrs = _load(chip, "芯片交叉")
        if chip_data is None:
            errs.extend(cerrs)
        else:
            from .chip_gen import line_names
            chip_lines = set(line_names(chip_data))
            here = {n for n in names if isinstance(n, str)}
            if missing := chip_lines - here:
                errs.append(f"[交叉] 连线配置缺少线路：{sorted(missing)}")
            if extra := here - chip_lines:
                errs.append(f"[交叉] 连线配置多出线路：{sorted(extra)}")
            if meta.get("chip") and chip_data["meta"].get("id") \
                    and meta["chip"] != chip_data["meta"]["id"]:
                warnings.append(f"[交叉] meta.chip={meta['chip']!r} 与芯片文件 "
                                f"id={chip_data['meta']['id']!r} 不一致")

    return errs, warnings


# ============================ CLI ============================

def main() -> None:
    args = sys.argv[1:]
    if len(args) < 2:
        print("用法：python -m qxtrl_timing_cali.validate chip|wiring <file> "
              "[--chip <chip.json>]")
        sys.exit(2)
    kind, path = args[0], args[1]
    chip = args[args.index("--chip") + 1] if "--chip" in args else None
    if kind == "chip":
        errs, warns = validate_chip(path)
    elif kind == "wiring":
        errs, warns = validate_wiring(path, chip)
    else:
        print(f"未知类型：{kind}")
        sys.exit(2)
    print(f"=== 校验（{kind}）: {path} ===" + (f"（交叉: {chip}）" if chip else ""))
    for w in warns:
        print(f" [warn]  {w}")
    for e in errs:
        print(f" [ERROR] {e}")
    if not errs and not warns:
        print(" [ok] 无问题")
    print(f"\nerrors={len(errs)} warnings={len(warns)} -> {'PASS' if not errs else 'FAIL'}")
    sys.exit(0 if not errs else 1)


if __name__ == "__main__":
    main()
