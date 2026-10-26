# -*- coding: utf-8 -*-
"""
queue_io.py — 执行方适配层（实验由其他软件执行）

两种对接方式共用同一接口：
  * DirectExecutor —— 同进程调用（模拟执行器、单元测试）；
  * FileQueue      —— 文件队列（跨进程 / 跨软件）：
        runs/requests/<id>.json  审查中心写出申请
        runs/results/<id>.json   执行方写回结果，本方读取后移入 runs/consumed/

真实测控软件只需能读 requests、能写 results，即与本仓库解耦。
results 建议按「临时文件 + 改名」落盘；直接写出的半截文件 poll 会宽限数秒后重试，
超时仍解析不了才归档 rejected/。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .contracts import ExperimentRequest, ExperimentResult, WritePlan

# 结果文件落盘后多久之内视为"可能还在写"：解析失败先别归档，等下一轮再看。
FRESH_WINDOW_S = 2.0


class ExecutorAdapter:
    """执行方接口：提交申请、取回结果；可选把批准的回写作用到设备。"""

    def submit(self, request: ExperimentRequest) -> None:
        raise NotImplementedError

    def poll(self, timeout_s: float | None = None) -> list[ExperimentResult]:
        raise NotImplementedError

    def submit_many(self, requests) -> None:
        """一次提交多条申请（并行波的提交束用）。

        默认逐条 `submit()`——同步执行方无需改动；真实测控软件可按自身能力覆盖，
        让一束实验在设备上重叠起来（也可能因 `hardware_hold` 而只是排队）。
        """
        for req in requests:
            self.submit(req)

    def inflight(self) -> list[str]:
        """已提交、尚未回国的申请 id 列表。

        同步执行方没有"在途"概念，默认返回空——台账据此判断"没有更多结果要等了"。
        """
        return []

    def apply(self, plan: WritePlan) -> None:
        """默认无操作：真实设备的延时下发由测控软件自身完成。"""

    def close(self) -> None:
        """默认无操作。"""


def _as_result(payload) -> ExperimentResult:
    return payload if isinstance(payload, ExperimentResult) else ExperimentResult.from_dict(payload)


class DirectExecutor(ExecutorAdapter):
    """同进程执行方：target 提供 run(request) → ExperimentResult | dict。"""

    def __init__(self, target, *, apply_shifts: bool = True):
        self.target = target
        self._apply = apply_shifts and hasattr(target, "apply_plan")
        self._inbox: list[ExperimentResult] = []

    def submit(self, request: ExperimentRequest) -> None:
        self._inbox.append(_as_result(self.target.run(request)))

    def poll(self, timeout_s: float | None = None) -> list[ExperimentResult]:
        out, self._inbox = self._inbox, []
        return out

    def apply(self, plan: WritePlan) -> None:
        if self._apply:
            self.target.apply_plan(plan)


def _write_json(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def _archive(src: Path, dst: Path, *, attempts: int = 3, delay: float = 0.05) -> None:
    """移档的短重试版（Windows 索引器/杀毒瞬时占用时与写回同一套纪律）。"""
    for i in range(attempts):
        try:
            target = dst
            if target.exists():          # 重复投递同一 id：不覆盖既有留痕，追加序号
                stem, suffix = dst.stem, dst.suffix
                for n in range(1, 1000):
                    cand = dst.with_name(f"{stem}-{n}{suffix}")
                    if not cand.exists():
                        target = cand
                        break
            os.replace(src, target)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(delay)


class FileQueue(ExecutorAdapter):
    """文件队列适配器（不做执行，只做交接）。"""

    def __init__(self, root="runs", *, subdirs=("requests", "results", "consumed", "rejected")):
        self.root = Path(root)
        self.requests, self.results = self.root / subdirs[0], self.root / subdirs[1]
        self.consumed, self.rejected = self.root / subdirs[2], self.root / subdirs[3]
        for d in (self.requests, self.results, self.consumed, self.rejected):
            d.mkdir(parents=True, exist_ok=True)
        self.errors: list[str] = []
        self._open: set[str] = set()        # 已提交、尚未取回结果的申请 id

    def submit(self, request: ExperimentRequest) -> Path:
        self._open.add(request.id)
        return _write_json(self.requests / f"{request.id}.json", request.to_dict())

    def poll(self, timeout_s: float | None = None) -> list[ExperimentResult]:
        out = []
        for p in sorted(self.results.glob("*.json")):
            try:
                result = ExperimentResult.from_dict(json.loads(p.read_text(encoding="utf-8")))
            except json.JSONDecodeError as exc:
                # 解析失败且文件刚落盘：很可能是执行方还在写的半截文件——
                # 留在原地等下一轮，别急着归档 rejected（那等于把测量弄丢）。
                if time.time() - p.stat().st_mtime < FRESH_WINDOW_S:
                    continue
                self.errors.append(f"{p.name}: {exc}")
                _archive(p, self.rejected / p.name)
                continue
            except Exception as exc:  # noqa: BLE001 — 契约不合规格式繁多（TypeError/KeyError…），一律留档不阻塞
                self.errors.append(f"{p.name}: {exc}")
                _archive(p, self.rejected / p.name)
                continue
            _archive(p, self.consumed / p.name)
            out.append(result)
            self._open.discard(result.request_id)
        return out

    def inflight(self) -> list[str]:
        """还在设备/执行方那一侧没回来的申请（已提交且未取回）。"""
        return sorted(self._open)

    def publish_result(self, result: ExperimentResult) -> Path:
        """供执行方（或测试）写结果。"""
        return _write_json(self.results / f"{result.request_id}.json", result.to_dict())

    def pending_requests(self) -> list[ExperimentRequest]:
        return [ExperimentRequest.from_dict(json.loads(p.read_text(encoding="utf-8")))
                for p in sorted(self.requests.glob("*.json"))]
