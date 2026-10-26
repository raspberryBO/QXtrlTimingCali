# 接入真实实验：执行方适配教程

面向后期把真实测控软件（QXtrl 等）接到本校准流程的读者。
核心只有一句话：**实现三个入口函数，其他一切（选边、重试、求解、写回、守卫）都不用管。**

| 入口 | 何时被调用 | 你要做什么 |
| --- | --- | --- |
| `submit(request)` | 每次申请实验 | 按申请组织并（异步或同步）执行实验 |
| `poll()` | `submit` 之后**立刻一次** | 返回已完成的结果列表 |
| `apply(plan)` | 写回落盘之后（可选） | 让设备跟随批准的位移 |

本文所有示例代码都在本仓库跑通过（8 比特 chain 芯片 · 31 条线 · 四阶段全流程 ·
`aligned=True` · 残差 ≈0.3–0.4 ns），可直接照抄改写。
接口定义见 `qxtrl_timing_cali/review/queue_io.py`（适配层）与
`qxtrl_timing_cali/review/contracts.py`（消息契约）；参考实现见 `qxtrl_timing_cali/sim.py` 的 `SimExecutor`。

---

## 1. 校准期间到底发生什么（调用链）

编排器（`StagedCalibrationPipeline`）把实验分成四组顺序执行；每一组里，
`MeasurementLedger`（`review/ledger.py`）逐条边做同一件事：

```text
session.new_request(kind, pair.a, pair.b, params)   # 生成 ExperimentRequest
       ↓                                             （id 形如 req-007-align，
executor.submit(req)                                #  config_hash = 当前待测文件指纹）
       ↓
executor.poll()                                     # ← 只调用一次，按 request_id 匹配
       ↓
result.config_hash == session.config_hash ?          # 过期检测：不等 → 整批作废停手
       ↓
quality_problems(result, thresholds) 为空 ?          # 五条质量判据（见 §3.3）
       ↓ 合格                        ↓ 不合格
记录进台账                        重试（同一条边发新申请，默认最多 3 次）
                                    用尽 → 坏边 → 替补边重连 / 隔离区
```

测量阶段（组 2、组 3）**一个字节都不写**——所有 skew 描述同一时刻的物理状态，
这是末尾全局最小二乘成立的前提。只有即时组（组 1 读取线、末尾收尾）测完立刻：
四道守卫落盘 pending 文件 → 会话推进指纹 → `executor.apply(plan)` 让设备跟随。

**对执行方的含义**：一次 `submit` + 一次 `poll` 就是一次完整实验；
你不需要理解分组和求解，只需要把每次申请测准、如实报告。
每条边测完，审查中心会自动往 pending 旁的测量日志追加一行（见 §11）——
真机长时间测量期间的崩溃恢复由此保障，执行方无需参与。

---

## 2. ExperimentRequest —— 你收到什么

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | str | 申请编号，如 `req-007-align`。结果必须原样回填到 `request_id` |
| `kind` | str | 当前恒为 `"timing.align"`；结果原样回填 |
| `targets` | `[new, ref]` | 要比较的两条线。**符号约定的核心：skew = q(new) − q(ref)，new = targets[0]** |
| `config_hash` | str | 申请时待测文件指纹。**必须原样回填**（过期检测用，见 §9 坑 1） |
| `intent` | dict | 审查中心内部信息（块合并意图），执行方可忽略 |
| `params` | dict | 实验参数，从 `ReviewCenter(params=...)` 透传：`n_shots` / `repeats` / 自定义协议参数（见 §8.2） |
| `note` | str | 人类可读说明，如 `Q0 xy-z 对齐` |

线名规则（`README` 约定）：`<Q>.xy` / `<Q>.z`（比特）、`<CP>.z`（耦合器）、
`<RL>.ro` / `<RL>.ri`（读取线发射 / 采样）。申请里出现的线一定存在于待测文件。

---

## 3. ExperimentResult —— 你必须返回什么

### 3.1 字段

**必填**（缺一即契约不合格，`from_dict` 直接抛错）：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `request_id` | str | 原样回填 `request.id` |
| `kind` | str | 原样回填 `request.kind` |
| `config_hash` | str | 原样回填 `request.config_hash` |
| `measured_at` | str | ISO 时间戳 |
| `skew_ns` | float | **主判据**：q(new) − q(ref)，带符号。new 在 ref 左边则为负 |
| `sigma_ns` | float | skew 的不确定度（标准误，ns） |
| `quality` | float | 主评分 0–1（对比度 / 拟合质量的综合自评） |

**可选**：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `flags` | `list[str]` | 自报异常标志，受白名单约束（§3.4） |
| `n_shots` | int | 总射击数 |
| `repeats` | `list[dict]` | 每次重复的 `{"skew_ns": ...}`（可再加 `sigma_ns` / `quality`），用于自洽性检查 |
| `artifacts` | dict | 证据**只放路径与哈希**（`{"raw": "...", "plot": "...", "sha256": "..."}`），不内联数组 |
| `extra` | dict | 其他自由信息 |

同进程方式下返回 `dict` 也可以（字段名相同，适配层会自动转换）。

### 3.2 sigma 与 quality 怎么给

- `sigma_ns` 有**双重身份**：既是质量判据（§3.3），又是全局求解的权重 `1/σ²`。
  **报小了会让这条边在最小二乘里权重虚高、污染全局解**——按拟合的真实标准误如实报告。
- `quality` 没有规定算法，参考实现（`sim.py`）用对比度衰减模型
  `quality = exp(−(σ / quality_ref)²)`，`quality_ref_ns` 可经 params 调。
  判据只有下限 `quality_min`（默认 0.6），你完全可以换成自己的对比度 / 拟合优度评分。

### 3.3 质量判据（`policies.quality_problems`，五条全过才算合格）

| 判据 | 默认阈值 | 说明 |
| --- | --- | --- |
| `sigma_ns ≤ sigma_max_ns` | 1.0 | 单次测量不确定度上限 |
| `quality ≥ quality_min` | 0.6 | 主评分下限 |
| `abs(skew_ns) ≤ skew_max_ns` | 1500 | 物理合理范围（超出视为测量异常） |
| 重复极差 `≤ spread_max_ns` | 2.0 | `repeats` 里 max−min（自洽性） |
| flags 无致命、无未知 | — | 见 §3.4 |

阈值全部集中在 `Thresholds`（`policies.py`），**当前都是量级占位值**，
真机标定后替换（路线图已列）。

### 3.4 flags 白名单（两级，名单外一律按异常处理）

| 级别 | 名单 | 处置 |
| --- | --- | --- |
| `KNOWN_FLAGS` | `drift` / `low_snr` / `low_contrast` / `crowded` | 只记录不判废，仍由 σ 与 quality 说话 |
| `FATAL_FLAGS` | `saturation` / `fit_failed` / `hardware_error` | 直接判废该次结果 |
| 其他 | 任何不在名单内的 | 视为异常结果——宁可不写，也不把看不懂的数据落盘 |

---

## 4. WritePlan 与设备跟随

写回落盘后，编排器会调 `executor.apply(plan)`，`plan` 关键字段：

- `shifts`：线名 → 右移量（ns，恒 ≥ 0，只增不减）；
- `headroom_return`：线名 → 收回量（恒 ≥ 0），**唯一允许的负向通道**，
  只用于收回流水线先前主动授予的 readout headroom；
- **净位移 = `shifts − headroom_return`**，逐线可能为负（仅限 headroom 收回场景）。

两个必须知道的语义：

1. **dry-run 也会 apply 到设备**。`--dry-run` 只是不写 pending 文件；
   设备侧照常跟随（否则后续组测到的 skew 属于另一个世界，末尾统一求解会解歪）。
   真机联调时记住：dry-run ≠ 设备不动。
2. **`FileQueue.apply()` 是 no-op**。跨进程方式下，位移下发的责任在测控软件一侧，
   同步协议见 §6.3。

权威状态是待测文件（pending）：每次写回都是临时文件 + `os.replace` 原子替换，
保留 `.bak`，并在同目录追加 `writeback-audit.jsonl`（逐线 `before/delta/after` 可回放）。

---

## 5. 接入方式 A：同进程（`DirectExecutor`）

你的软件能以 Python 模块形式被 import 时用这个。只需实现
`run(request)`（必需）和 `apply_plan(plan)`（建议）：

```python
import math, statistics
from datetime import datetime
from qxtrl_timing_cali import (CalibrationSession, DirectExecutor, ExperimentResult,
                               ReviewCenter, StagedCalibrationPipeline, Thresholds,
                               generate_chip, generate_wiring, save_wiring)

class LabExecutor:
    """真实测控软件的适配层：run() 测量、apply_plan() 下发位移。"""

    def run(self, request) -> ExperimentResult:
        new, ref = request.targets                  # skew = q(new) − q(ref)
        n = int(request.params.get("n_shots", 1000))
        # ↓↓↓ 这里换成真实实验：扫描 + 拟合（协议选择见 §8）↓↓↓
        samples = [my_measure_once(new, ref, request.params) for _ in range(n)]
        skew = statistics.fmean(samples)
        sigma = statistics.stdev(samples) / math.sqrt(n)
        # ↑↑↑ 以及真实的 sigma / quality（拟合不确定度、对比度评分）↑↑↑
        return ExperimentResult(
            request_id=request.id,                  # 原样回填
            kind=request.kind,
            config_hash=request.config_hash,        # 原样回填（过期检测）
            measured_at=datetime.now().isoformat(timespec="seconds"),
            skew_ns=round(skew, 6), sigma_ns=round(sigma, 6),
            quality=round(math.exp(-(sigma / 0.5) ** 2), 6),   # 参考 definition，可换
            n_shots=n)

    def apply_plan(self, plan) -> None:
        net = dict(plan.shifts)
        for ln, ret in plan.headroom_return.items():
            net[ln] = net.get(ln, 0.0) - ret        # 净位移 = shifts − headroom_return
        for ln, delta in net.items():
            my_device_add_delay(ln, delta)          # 下发到真实设备

# ---- 组装（真实场景没有 truth 文件，run() 不传即可） ----
chip = generate_chip(num_qubits=8, seed=42)
pending = "runs/wiring-pending.json"
save_wiring(generate_wiring(chip, kind="pending"), pending)

session = CalibrationSession.from_pending(pending)
center = ReviewCenter(session, DirectExecutor(LabExecutor()),
                      thresholds=Thresholds(tol_ns=0.5, d_max_ns=5000),
                      params={"n_shots": 1000})
report = StagedCalibrationPipeline(chip, center).run()
print(report["aligned"], report["isolated"], report["aborted"])
```

同进程参考实现 `SimExecutor`（`sim.py`）就是按这个形状写的。
用假数据源替换 `my_measure_once` 后，本文验证时输出：
`stages = [readout, qubit, coupler, readout_final] · aligned = True · 残差 ≈0.2–0.5 ns`
（随芯片与种子而变，`runs/demo/` 的 notebook 输出里有逐位数字）。

---

## 6. 接入方式 B：跨进程 / 跨软件（`FileQueue`）

测控软件独立进程、甚至不依赖本仓库时用这个。两边只认文件：

| 目录 | 谁写 | 谁读 | 生命周期 |
| --- | --- | --- | --- |
| `runs/requests/<id>.json` | 审查中心（`submit`） | 测控软件 | **执行方处理完自行删除/归档**（中心不再管） |
| `runs/results/<id>.json` | 测控软件 | 审查中心（`poll`） | 读完自动移入 `consumed/`；契约不合格移入 `rejected/` 不阻塞 |
| `runs/consumed/`、`runs/rejected/` | 自动 | — | 留档 |

消息体就是 §2 / §3 的 `to_dict()` 形式（JSON、UTF-8）。

### 6.1 测控软件一侧的最小循环

```python
import json, math, statistics, time
from datetime import datetime
from pathlib import Path

REQ, RES = Path("runs/requests"), Path("runs/results")
PENDING = Path("runs/wiring-pending.json")

while True:
    for p in sorted(REQ.glob("*.json")):
        req = json.loads(p.read_text(encoding="utf-8"))
        # ① 处理申请前：把 pending 的 d 同步到设备（幂等，见 §6.3）
        delays = {e["line"]: float(e["delay"])
                  for e in json.loads(PENDING.read_text(encoding="utf-8"))["lines"]}
        for ln, d in delays.items():
            device_set_delay(ln, d)                 # 按文件绝对值设置（非增量）
        # ② 测量：skew = q(new) − q(ref)，协议按 targets 线名分发（§8.1）
        new, ref = req["targets"]
        skew, sigma, quality, n = my_experiment(new, ref, req["params"])
        # ③ 原子写结果（先 tmp 再 replace，避免读到半截文件）
        result = {"request_id": req["id"], "kind": req["kind"],
                  "config_hash": req["config_hash"],      # 原样回填
                  "measured_at": datetime.now().isoformat(timespec="seconds"),
                  "skew_ns": skew, "sigma_ns": sigma, "quality": quality, "n_shots": n}
        out = RES / p.name
        tmp = out.with_name(out.name + ".tmp")
        tmp.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(out)
        p.unlink()                                   # 取走申请
    time.sleep(0.5)
```

注：`*.json` glob 不会匹配 `.json.tmp`，读写双方的临时文件都安全。

### 6.2 坑：台账只 poll 一次 —— 审查中心一侧要用阻塞等待版

`MeasurementLedger` 在 `submit` 之后**只调用一次 `poll()`**（为同步执行方设计）。
真实实验要跑几分钟，直接配 `FileQueue` 会导致每次都"执行方未返回结果"→ 三次重试全烧完 →
全线进隔离区。**同进程这一侧请包一层阻塞等待**：

```python
import time
from qxtrl_timing_cali import FileQueue

class BlockingFileQueue(FileQueue):
    """submit() 写出申请后阻塞等该申请的结果出现（串行流水线下安全）。"""

    def __init__(self, root, *, poll_interval_s=2.0, timeout_s=3600.0):
        super().__init__(root)
        self.interval, self.timeout_s = float(poll_interval_s), float(timeout_s)
        self._stash = []

    def submit(self, request):
        super().submit(request)
        deadline = time.monotonic() + self.timeout_s
        while time.monotonic() < deadline:
            self._stash.extend(super().poll())
            if any(r.request_id == request.id for r in self._stash):
                return
            time.sleep(self.interval)
        raise TimeoutError(f"等待 {request.id} 结果超时（>{self.timeout_s:g}s）")

    def poll(self):
        out, self._stash = self._stash, []
        return out + super().poll()

center = ReviewCenter(session, BlockingFileQueue("runs"), ...)
```

### 6.3 坑：`FileQueue.apply()` 是 no-op —— 设备同步是测控侧的责任

跨进程方式下位移不会自动下发。同步协议很简单，就是 §6.1 循环里的第 ① 步：

> **处理每张申请之前，把 pending 文件的 delays 按绝对值设置到设备。**

正确性依据：编排器保证一张申请被 `submit` 时，pending 文件就是
`request.config_hash` 对应的那个版本（即时组写回 → 指纹推进 → 后续申请带新 hash）。
所以"申请到达 → 同步文件 → 测量"测到的必然是申请所指的世界。
按绝对值设置（而非增量）保证幂等，重复同步无副作用。
额外校验（可选）：复刻 `writeback.content_hash`（对 pending 规范化 JSON 取 sha256 前 16 位），
同步前核对指纹与 `request.config_hash` 一致。

本文验证时，这一模式（独立"实验软件"线程 + `BlockingFileQueue`）端到端跑通四阶段：
`aligned = True · 残差 0.277 ns · written = True`。

---

## 7. 单对调试：手动四技能

不跑编排器、只测一对线时，用审查中心的四个接口
（`request_align` / `collect` / `review` / `pump`）：

```python
session = CalibrationSession.from_pending("runs/wiring-pending.json")
center = ReviewCenter(session, DirectExecutor(LabExecutor()), params={"n_shots": 1000})

session.ensure_block("Q0.z", ["Q0.z"])       # 手动场景先注册单例块（编排器会自动做）
session.ensure_block("Q0.xy", ["Q0.xy"])
req = center.request_align("Q0.z", "Q0.xy", note="手动单对调试")
print(req.id, req.targets)                    # → req-001-align ['Q0.z', 'Q0.xy']

for v in center.pump():                       # 取回结果并走完整策略链
    print(v.action, "|", v.reason)
    if v.write_plan:
        print("  计划：", v.write_plan.shifts or "仅合并（容差内）")
```

验证输出：`commit | 批准写回：skew=-27.700ns → 右移块 Q0.z +27.700ns`。
决议动作含义：`commit` 落盘 · `noop` 容差内只合并 · `retry` 质量不达标重发 ·
`recheck` 结果过期 / 守卫拒绝 · `replan` 会话不支持 · `escalate` 用尽重试转人工。

---

## 8. 四类测量实验 ↔ 物理协议

### 8.1 分发表（靠 targets 线名模式区分）

`request.kind` 目前恒为 `"timing.align"`，分组信息只在编排层（`Pair.group` 不进消息），
执行方按 `targets` 的线名模式分发到不同物理实验：

| targets 模式（new → ref） | 组 | 物理实验 | 参考 |
| --- | --- | --- | --- |
| `RL.ri → RL.ro` | 1 readout | 读出脉冲发射 vs 采样窗对准 | 项目扩展（无文献对应，需自研协议） |
| `Q.z → Q.xy` | 2 qubit | 单比特 timing：偏置离开 idle 点，扫 XY/Z 相对延时，布居数深谷 = 对准 | 该类文献 SM §2.A.3 single-qubit timing |
| `Qa.z → Qb.z` | 3 coupler | QQswap：共振偏置 + 标定交换周期 T，t_swap ≈ T/4 处布居数峰 = 对准 | 同上 QQswap |
| `CP.z → Q.z`（或 `Q.xy`） | 3 coupler | QQswap_C：交换工作点上扫耦合器偏置解耦，深谷 = 对准 | 同上 QQswap_C |
| `RL.ro → Q.xy` | 4 readout_final | 读取线世界 ↔ 比特世界的桥接测量（步 1 之后 ro/ri 同步增减，故**每线只测一条边**，`ri` 的位移由编排层按同量补上） | 自研协议 |

方向上无需关心"谁该等谁"：求解只会产出 δ ≥ 0 的右移，
执行方只负责把带符号的 `skew = q(new) − q(ref)` 测准。

### 8.2 协议参数放 `params`

批量模式的特点是**全部边都在初始基线上测**（中间不写回），所以单次实验的搜索窗
必须覆盖初始失配全量（≈ 各线延时 L 的极差；真机即线缆 / 器件延时差）。
文献流程每次只测残余失配、±50 ns 窗口就够，这里没有这个便利。建议在
`ReviewCenter(params=...)` 里为执行方声明协议参数，例如：

```python
center = ReviewCenter(session, BlockingFileQueue("runs"),
                      params={"n_shots": 1000,
                              "scan_range_ns": 400,      # 粗扫总窗（覆盖 L 极差）
                              "scan_step_ns": 8.0,       # 粗扫步长
                              "fine_range_ns": 32.0,     # 粗定位后的精扫窗
                              "fine_step_ns": 0.5,
                              "t_swap_ns": None})        # 组 3：交换周期 T/4，由前置标定填
```

带三方上下文的实验（QQswap_C 物理上要激发 Qa、扫 CP.z、读 Qb）目前契约只有两根线，
伙伴比特可由芯片拓扑从 `CP.z` 的两个候选锚点推断，或经 `params` 显式携带。
这些键名是建议而非既定契约——执行方与审查中心共享一份约定即可。

---

## 9. 常见坑清单

1. **`config_hash` 忘记回声** → 台账判"待测文件在测量期间被改动"，
   **整批观测作废、流程中止**（不可重试）。这是最高频的接入错误。
2. **sigma 报小** → 求解权重 1/σ² 虚高，一条边绑架全局解。按拟合真实标准误报告。
3. **`FileQueue` 直配真实实验** → 台账只 poll 一次，慢实验必超时。用 §6.2 的 `BlockingFileQueue`。
4. **以为 dry-run 设备不动** → dry-run 只免写文件，`apply` 照发（§4）。
5. **复跑用旧 pending** → 模拟器每轮回 `q = L` 重启；真机同理，复跑前用
   `wiring_gen --kind pending` 重新生成（或确认设备已按当前文件同步）。
6. **阈值当真** → `Thresholds` 目前全是占位值，真机标定前 `aligned` 只具参考意义。
7. **自创 flags** → 名单外标志按异常处理，结果直接作废。先扩充 `KNOWN_FLAGS` /
   `FATAL_FLAGS`（`policies.py`）再用。
8. **结果 JSON 缺必填字段** → 落 `rejected/` 不阻塞，但对应边三次重试后进隔离区。

---

## 10. 上线前自测清单

按顺序做，每步都绿再进下一步：

- [ ] `python -m unittest discover -s tests` 全过（确认仓库本身健康）；
- [ ] `examples/demo_calibration.ipynb` Run All —— 用 `SimExecutor` 冒烟全流程；
- [ ] 用 §5 的假执行方（合成数据源）替换 `SimExecutor`，全流程 `aligned=True`；
- [ ] §6 跨进程模式：假"实验软件"进程 + `BlockingFileQueue`，全流程 `aligned=True`；
- [ ] `--dry-run` 联调：确认设备跟随、文件未动、审计无新记录；
- [ ] 真机小规模（2–3 比特）首跑：先看 `runs/pipeline-report.json` 的
      `measurements`（逐边 skew / 尝试次数）与 `failed` / `isolated`，
      再逐步放开规模；
- [ ] 首轮结束后回填 `Thresholds` 经验值（σ 上限、quality 下限、spread、skew 合理域）。

---

## 11. 崩溃恢复：测量日志与断点续跑

真机上一条边几分钟、一整批几小时，而批量流程把全部观测攒到末尾才一次写回——
**测量期崩溃的爆炸半径恰好最大**。审查中心一侧已内置测量日志，执行方无需做任何事：

- **写入**：每条边测完，自动往 pending 伴生文件 `<stem>.measurements-audit.jsonl`
  追加一行（skew / σ / quality / `config_hash` / 组名）；阶段完成时追加
  `stage_done` 事件（含写回前后的指纹与 headroom 授予信息）。追加写接近原子，
  崩溃残留的半截行读取时自动跳过。
- **恢复判据**（由版本纪律直接给出，无需新概念）：
  - 日志里 `config_hash` 与当前待测文件**一致**的合格观测，描述的正是眼前这个世界
    → 重启后按边跳过，只补测剩余的边；
  - `stage_done` 事件的测量基线指纹 ≠ 当前指纹 → 该阶段已随写回**整体生效**
    → 整阶段跳过（不重测不重写），headroom 授予台账随事件重放，
    末尾求解是确定性纯计算、用旧观测重算即可。
- **恢复操作**：什么都不用做——重新 `CalibrationSession.from_pending(pending)` 起
  流水线，preload/跳过全部自动。分组流水线（`StagedCalibrationPipeline`）里已完成的
  整轮重跑是零测量零写回；两阶段批量流水线（`CalibrationPipeline`）没有阶段事件，
  已写回的旧观测按 hash 作废，重跑会全量重测——它的恢复价值在**测量中途**崩溃。
- **开新一轮**：重新生成待测文件（`wiring_gen --kind pending`）即视为弃掉旧世界，
  伴生日志自动作废；新一轮从零开始测。
- 执行方一侧唯一要注意的：恢复后申请编号会接着日志里的最大号继续发
  （不会从 `req-001` 重发撞号），FileQueue 里崩溃前残留的孤儿申请文件
  会被同 id 的新申请原子覆盖，无须手工清理。


---

## 12. 校准只是你实验流程的一环：分阶段驱动

真实测控里，读完 `ro/ri` 之后还要插 S21 / spectrum / scatter / power Rabi 等实验，
剩下的组什么时候跑由**上层自动化控制中心**决定。所以分组流水线除 `run()`（一次跑完）
之外，另提供三个**每次只推进一步**的入口——它们**绝不会顺带把后面的组跑起来**：

| 入口 | 作用 |
| --- | --- |
| `pipe.stage_status()` | 只看不跑：每组 `state` / `ready` / `blocked_by` + `todo` 下一步建议 |
| `pipe.run_group(name)` | 只跑 `readout` / `qubit` / `coupler` / `readout_final` 之一；依赖不满足就拒绝 |
| `pipe.run_final(truth)` | 只跑步 5：延后组观测合并求解 → 整批一次写回 |

```python
pipe = StagedCalibrationPipeline(chip, center, readout_headroom_ns=100.0)
st = pipe.stage_status()
while st["todo"]:
    if st["todo"][0] == "final":
        pipe.run_final(truth)            # 步 5：整批一次写回
    else:
        pipe.run_group(st["todo"][0])    # 跑一组就停——中间随时可插入你自己的实验
    st = pipe.stage_status()             # 再问一次体检，决定下一步
```

CLI 同语义，方便外部调度器直接起进程：

```bash
python -m qxtrl_timing_cali.stages --chip chip.json --pending runs/p.json --status [--json]
python -m qxtrl_timing_cali.stages --chip chip.json --pending runs/p.json --stage readout --headroom 100
python -m qxtrl_timing_cali.stages --chip chip.json --pending runs/p.json --final
```

**执行方在两件事上必须兜住**（其余照旧）：

1. **设备状态必须与待测文件一致**。每次调用都是新进程，pending 里已有前几组的位移；
   模拟侧 CLI 调 `sim.seed_applied_shifts(device, delays)` 按 pending 的 `d` 还原 `SimChip`
   （两个 CLI 都这么做，所以复跑不会把上一轮的 `d` 叠上去），真实侧则靠你的 `apply(plan)`
   把批准并落盘的位移真正下发到板卡。否则新进程从 `q = L` 起算，测出来是另一个世界，
   末尾全局求解会把它们解歪。
2. **一次 `submit` + 一次 `poll` 仍然是一次完整实验**。跨进程驱动不改变这个约定
   （两阶段批量流水线 `CalibrationPipeline` 同样如此：测量期只读、末尾一次写回，
   崩溃后同版本观测从 `measurements-audit.jsonl` 恢复，报告里的 `resumed_edges` 就是恢复条数）；
   崩溃恢复也照旧：日志里 `config_hash` 与当前一致的观测自动跳过，已生效的阶段整段跳过，
   步 5 的全局解与 headroom 授予台账都随事件带走，步 6 单独驱动也能正确收回 headroom。

---

## 13. 并行策略：一个波 → 一批 QXtrl 提交

校准中心会为每组测量边算出一份**波划分**（冲突规则只有一条：两条边共享线名才冲突；
同一读取线上的多比特可多音读取，不算冲突），`report["waves"]` / 每个 stage 的 `waves` 里
能直接看到"几波、每波几条"。你的 LabExecutor 要做的事很简单：

```python
class LabExecutor(ExecutorAdapter):
    def submit_many(self, requests):
        # 一波：一次性交给 QXtrl 批量提交（SubmissionSupervisor 队列 / aio 异步）
        batch = QXtrl.batch()
        for req in requests:
            batch.add(self._recipe_for(req))     # targets 线名 → 物理 recipe
        self._inflight.update(r.id for r in requests)
        batch.submit_async(callback=self._on_done)

    def poll(self, timeout_s=None):
        out = list(self._done.values()); self._done.clear()
        return out

    def inflight(self):
        return list(self._inflight)
```

三条纪律：

1. **设备独占就老实排队**。`hardware_hold` 只允许一个实验在设备上时，`submit_many` 完全可以
   逐条跑完再返回——波只是提交提示，不是并发承诺，结果按 `request_id` 归位，中心侧无感。
2. **批准并落盘的位移必须真的下发**（`apply(plan)`）。多批次驱动时设备状态若与 pending 不一致，
   下一组测到的就是另一个世界——这是并行之外本来就成立的纪律。
3. **发现共测劣化就举手**。多音/共测导致 SNR 下降或串扰，请在结果里带
   `flags=["crowded"]`（或 `low_snr`）——这两者已在 `KNOWN_FLAGS` 白名单里，
   记录但不判废，最终由 σ 与 quality 说话；判据一行都不用改。
