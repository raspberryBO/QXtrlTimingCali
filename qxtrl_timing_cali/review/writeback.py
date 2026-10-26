# -*- coding: utf-8 -*-
"""
writeback.py — 守卫式回写工具（待测文件 line + delay）

四道守卫（缺一不可）：
  ① CAS：expected_hash 必须等于当前文件指纹，否则丢弃结果、要求重测；
  ② 单调：逐线校准位移只增不减（delta ≥ 0）；唯一例外是**收回本流水线先前
     授予的 readout headroom**（headroom_return，受授予余额约束，d 不会低于
     加 headroom 之前的校准基线）；
  ③ 预算：回写后每线累计 delay ≤ d_max_ns；
  ④ 原子：先写临时文件再 os.replace，保留 .bak，并追加 JSONL 审计（可回放）。

本模块只认「线名 → 右移量」，不关心同步块概念——块簿记在 CalibrationSession。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import time
from datetime import datetime
from pathlib import Path

SCHEMA_KEYS = ("schema_version", "units")


class WriteGuardError(RuntimeError):
    """守卫拒绝：文件未被修改。"""


def _atomic_replace(tmp: Path, path: Path, *, attempts: int = 3, delay: float = 0.05) -> None:
    """os.replace 的短重试版。

    Windows 上目标文件可能被索引器 / 杀软瞬时占用，os.replace 报拒绝访问（WinError 5）。
    这不是守卫该管的事（文件内容没变、没有冲突写入者），重试几次即可；
    仍失败则清理临时文件并照常抛出，由守卫记录"未写回"。
    """
    for i in range(attempts):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if i == attempts - 1:
                break
            time.sleep(delay)
    try:
        tmp.unlink()
    except OSError:
        pass
    raise PermissionError(f"原子替换重试 {attempts} 次仍被占用：{tmp.name} → {path.name}")


def content_hash(wiring: dict) -> str:
    """待测文件内容指纹（只覆盖影响时序的部分：kind / chip / lines）。

    meta.date 与 description 不参与，避免「写回时顺手更新日期」自撞 CAS。
    注意 line 条目的其余字段（如 channel）同样不参与——模拟流程的通道映射
    不影响时间校准；若日后把通道纳入时序语义，需同步重算全部指纹。
    """
    payload = {
        "schema_version": wiring.get("schema_version"),
        "units": wiring.get("units"),
        "kind": (wiring.get("meta") or {}).get("kind"),
        "chip": (wiring.get("meta") or {}).get("chip"),
        "lines": [{"line": e["line"], "delay": float(e["delay"])} for e in wiring["lines"]],
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def load_pending(path) -> tuple[dict, str]:
    """读取待测文件，返回 (内容 dict, 指纹)。结构不完整直接判错，不让 KeyError 漏出去。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    kind = (data.get("meta") or {}).get("kind")
    if kind != "pending":
        raise WriteGuardError(f"只回写待测文件（meta.kind=pending），当前 {kind!r}")
    lines = data.get("lines")
    if not isinstance(lines, list) or not lines or any(
            not isinstance(e, dict) or "line" not in e or "delay" not in e for e in lines):
        raise WriteGuardError("待测文件 lines 节缺失或结构不完整（应为非空的对象数组）")
    names = set()
    for entry in lines:
        name, delay = entry["line"], entry["delay"]
        if not isinstance(name, str) or not name or name in names:
            raise WriteGuardError("待测文件线名必须非空且唯一")
        names.add(name)
        if isinstance(delay, bool) or not isinstance(delay, (int, float)) \
                or not math.isfinite(delay) or delay < 0:
            raise WriteGuardError(f"待测文件延时必须有限且非负：{name}")
    return data, content_hash(data)


def read_delays(path) -> dict[str, float]:
    data, _ = load_pending(path)
    return {e["line"]: float(e["delay"]) for e in data["lines"]}


def _append_audit(audit_path: Path, record: dict) -> None:
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with audit_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def commit_shifts(
    pending_path,
    shifts: dict[str, float],
    *,
    expected_hash: str,
    d_max_ns: float | None = None,
    request_id: str = "",
    reason: str = "",
    audit_path="runs/writeback-audit.jsonl",
    keep_backup: bool = True,
    headroom_return: dict[str, float] | None = None,
    granted_headroom: dict[str, float] | None = None,
) -> dict:
    """把「线名 → 右移量」写入待测文件，通过四道守卫才落盘。

    headroom_return（线名 → 收回量）是唯一允许负向修改 delay 的通道：
    逐线收回量不得超过 granted_headroom（会话授予的 headroom 余额），
    且逐线净位移 = shifts − headroom_return。

    返回回执：{old_hash, new_hash, applied:[{line,before,delta,after}], audit}
    任一守卫不过 → 抛 WriteGuardError，文件保持原样（审计仍留痕）。
    """
    path = Path(pending_path)
    shifts = {str(k): float(v) for k, v in shifts.items()}
    ret = {str(k): float(v) for k, v in (headroom_return or {}).items()}
    granted = {str(k): float(v) for k, v in (granted_headroom or {}).items()}
    audit = Path(audit_path)
    receipt: dict = {"pending": str(path), "request_id": request_id, "reason": reason,
                     "ts": datetime.now().isoformat(timespec="seconds"), "ok": False}

    data, old_hash = load_pending(path)
    receipt["old_hash"] = old_hash
    if old_hash != expected_hash:
        receipt.update(error=f"CAS 失败：文件已变（{old_hash} ≠ {expected_hash}）",
                       stale_shifts=shifts)
        _append_audit(audit, receipt)
        raise WriteGuardError(receipt["error"])

    def _fail(msg: str, **extra) -> None:
        """守卫拒绝统一出口：审计先留痕，再抛出——文件保持原样。"""
        receipt.update(error=msg, **extra)
        _append_audit(audit, receipt)
        raise WriteGuardError(msg)

    # ②前置：NaN 与任何数比较都是 False，单调/预算守卫对它全部失明，必须先挡下
    if d_max_ns is not None and (not math.isfinite(d_max_ns) or d_max_ns < 0):
        _fail("预算上限必须有限且非负")
    for _label, vals in (("右移量", shifts), ("headroom 收回量", ret), ("headroom 余额", granted)):
        for ln, v in vals.items():
            if not math.isfinite(v) or v < 0:
                _fail(f"{_label}不是有限数值：{ln} = {v}（NaN 会穿透单调与预算守卫）")

    by_line = {e["line"]: e for e in data["lines"]}
    applied = []
    for line in sorted(set(shifts) | set(ret)):
        entry = by_line.get(line)
        if entry is None:
            _fail(f"未知线路：{line}（不在 {path.name} 中）")
        if shifts.get(line, 0.0) < 0:
            _fail(f"只允许右移：{line} delta={shifts.get(line, 0.0)}")
        if ret.get(line, 0.0) < 0:
            _fail(f"headroom 收回量不可为负：{line} {ret.get(line, 0.0)}")
        if ret.get(line, 0.0) > granted.get(line, 0.0) + 1e-9:
            _fail(f"{line} 收回 headroom {ret.get(line, 0.0):.3f}ns 超过授予余额 "
                  f"{granted.get(line, 0.0):.3f}ns")
        before = float(entry["delay"])
        after = round(before + shifts.get(line, 0.0) - ret.get(line, 0.0), 6)
        if not math.isfinite(after) or after < 0:
            _fail(f"回写后延时必须有限且非负：{line} d={after}")
        if d_max_ns is not None and after > d_max_ns + 1e-9:
            _fail(f"超出预算：{line} d={after} > d_max={d_max_ns}",
                  requested={k: round(v, 6) for k, v in shifts.items()})
        delta = round(after - before, 6)
        if delta != 0:
            applied.append({"line": line, "before": before, "delta": delta,
                            "shift": round(shifts.get(line, 0.0), 6),
                            "headroom_return": round(ret.get(line, 0.0), 6),
                            "after": after})
            entry["delay"] = after

    receipt.update(applied=applied, written=bool(applied))
    if not applied:
        receipt["note"] = "无可写回位移（noop / 容差内）"
        _append_audit(audit, receipt)
        return receipt

    tmp = path.with_suffix(path.suffix + ".tmp")
    blob = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    tmp.write_text(blob, encoding="utf-8")
    # ①复核：load 之后、replace 之前文件可能被并发写者改过（读-验-写并非原子）。
    # 落盘前再取一次指纹，窗口收窄到复核与 replace 之间；不等就不写，宁可重测。
    _, current_hash = load_pending(path)
    if current_hash != old_hash:
        try:
            tmp.unlink()
        except OSError:
            pass
        _fail(f"CAS 复核失败：回写期间文件被并发修改（{current_hash} ≠ {old_hash}）")
    if keep_backup:
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
    try:
        _atomic_replace(tmp, path)     # 原子替换（同目录，含瞬时占用的短重试）
    except PermissionError as exc:
        receipt.update(error=f"原子替换失败（文件保持原样）：{exc}")
        _append_audit(audit, receipt)
        raise

    new_data, new_hash = load_pending(path)
    receipt.update(ok=True, new_hash=new_hash,
                   written_bytes=len(blob.encode("utf-8")))
    _append_audit(audit, receipt)
    return receipt
