# AGENTS.md

超导量子比特 Timing 校准系统。**零第三方依赖**（Python ≥ 3.10 标准库），不要引入第三方包。
仓库语言为中文：README、docstring、注释、提交信息均用中文，新代码保持一致。

## 必读文档

- `docs/learning-path.md` — 新接手代码先读：分层依赖地图、每个模块的关键不变量、常见陷阱（§6）
- `README.md` — 本身就是架构文档：模块表、四组六步、四道回写守卫、路线图
- `docs/executor-integration.md` — 对接真实执行方（FileQueue / 设备同步协议）时的操作手册

## 结构与分层

依赖只能从上往下（learning-path §2 有完整地图）：

- 第 0 层 `qxtrl_timing_cali/review/contracts.py` — 五种 JSON 消息，全系统通用语言，必须逐行理解
- 第 1 层 `chip_gen.py` / `wiring_gen.py` / `validate.py` / `visualize.py` — 静态工具，互相独立，只认 JSON 规格
- 第 2 层 `review/` 其余模块 — session（d 簿记）· policies（策略链）· graph（选边）· solver（最小二乘）· writeback（守卫）· ledger（测量台账）· queue_io · parallel（波划分）· center
- 第 3 层 编排器 `sim.py` → `pipeline.py`（两阶段不分组）→ `stages.py`（四组六步，全仓库最复杂，最后读）
- 第 4 层 `tests/` 与 `docs/gen_*.py` — 测试当可执行规格用；`docs/*.html` 动画由 gen 脚本从真跑数据生成

注意：`pipeline.py` 与 `stages.py` 有大段近亲代码（`_write` / `_reconnect` / 报告组装）。
改动时两边对照——历史上的真实缺陷（连通性过滤只加一边）就是这么产生的。

## 常用命令

```bash
# 全部测试（unittest 风格，也可用 pytest 跑，约 7 秒）
python -m unittest discover -s tests -v

# 生成示例（--id / --seed 必给，命令才能复现入库文件）
python -m qxtrl_timing_cali.chip_gen --id chip-8q --qubits 8 --seed 42 --out examples/chip-8q.json
python -m qxtrl_timing_cali.wiring_gen --chip examples/chip-8q.json --kind truth --seed 7 --out examples/wiring-8q-truth.json
python -m qxtrl_timing_cali.wiring_gen --chip examples/chip-8q.json --kind pending --out examples/wiring-8q-pending.json

# 交叉校验 / 可视化
python -m qxtrl_timing_cali.validate wiring examples/wiring-8q-truth.json --chip examples/chip-8q.json
python -m qxtrl_timing_cali.visualize --chip examples/chip-8q.json --wiring examples/wiring-8q-truth.json --out examples/chip-8q.html
```

无 lint / typecheck 配置。

## 硬性不变量（动核心代码前必须知道）

- 符号约定：`skew = q(new) − q(ref)`，全仓库的符号推导建立在它上面；每线位置 `q = L + d`，
  `d` 只增不减，唯一合法负向通道是 `WritePlan.headroom_return`
- 回写四道守卫（CAS / 单调 δ≥0 / 预算 d_max / 原子写）缺一不可；任一不过 → 文件一个字节不动、审计留痕
- NaN 在契约入口（`from_dict`）拒收：NaN 与任何数比较都是 False，会让阈值与守卫全部失明
- `solver.solve()` 只吃**单个连通分量**；不连通时只解最大分量、其余进隔离区（isolated）。
  多链芯片要用 `extra_edges` 补桥接边才能全局求解
- 设备状态必须等于待测文件状态：新进程要按 pending 当前的 `d` 还原设备
  （`sim.seed_applied_shifts`，CLI 已内置）；直接 `SimChip(truth)` 不还原 = 测另一个世界
- 写回一旦发生，旧观测全部作废（CAS 纪律）；测量日志按 `config_hash` 判断观测是否仍描述当前世界
- 并行的唯一冲突判据 = 两条边共享线名（共享读取线**不算**冲突，多音读取）；波是提交提示，
  不是并发承诺——求解、写回、设备下发始终串行
- 线名约定：`<Q>.xy` / `<Q>.z` / `<CP>.z` / `<RL>.ro` / `<RL>.ri`，模块按后缀分类
- 阈值集中在 `review/policies.py` 的 `Thresholds`，目前是占位值，待真实经验值替换

## 改动后的义务

- **改任何影响流水线数值行为的代码后，必须重新生成两份演示数据**（再生测试会逐字段对账）：
  `python docs/gen_animation_data.py --chip-day <旧日期>` → `python docs/gen_animation.py`；
  `python docs/gen_parallel_data.py` → `python docs/gen_parallel_animation.py`
- 改架构图/说明：`python docs/gen_architecture.py`（图版本独立于包版本）
- `runs/` 整目录被 gitignore（工作产物：session、审计 JSONL、`.bak`）；`*.html` 除 `docs/*.html` 外也忽略
- 演示数据生成必须钉住日期（`generate_chip` 缺省写"今天"，日期漂移会挂再生测试）
