# 给 1 号、3 号的基线交接

这份文档保留原基线交接入口及当时背景。当前已完成至第十三轮本机实验与独立审计，最新状态以[当前状态](docs/CURRENT_STATUS.md)和[实验记录](EXPERIMENTS.md)为准。用户已同机接管原1/3/4号职责；原“未收到1号数据、待3号外部确认”属于接管前快照，不作为当前等待条件，也不代表其他成员或异机验收完成。正式规则和接收环境问题见[清单](docs/team_questions.md)。真实数据留在本地，仓库只含源码、配置、文档与明确标注的合成示例。新增原生无故障入口见[独立契约](docs/NO_FAULT_NATIVE.md)；以下原基线/故障交接命令保留其原用途。

## 环境与启动

已有环境直接使用，无需重建或升级。下面在 PowerShell 执行：

```powershell
# 在本机项目根、已激活的既有环境中执行。
$py = 'python'
& $py -X utf8 smoke_test.py --config configs/smoke.yaml --method baseline --out outputs/baseline_delivery_01
```

上面输入默认是本地 `prototype_v1/data_raw` 和原冻结 seed42 样本。换电脑不应依赖这个本地目录。新 Python 3.11 环境可 `python -m pip install -r requirements.txt`；运行测试另需 pytest。原有 `requirements-lock.txt` 是本机完整环境清单，本轮保持原字节，不作为最小必装依赖。

没有真实数据也可运行合成示例：

```powershell
& $py -X utf8 smoke_test.py --config configs/synthetic.yaml --method baseline --out outputs/synthetic_delivery_01
```

`--out` 必须是新目录；再次运行更换末尾编号。每个案例由可控子进程执行，总预算最多 300 秒；超时返回 3，保留部分文件并终止进程树，不自动重试。

## 1 号标准窗口接入

提供 `targets.csv`、`stations.csv`、`windows.csv`、`metadata.json`，契约见 [io_contract.md](docs/io_contract.md)。从 `configs/synthetic.yaml` 复制配置，将 `input_dir`、`station_ids`、UTC 起点及时长改成收到的数据；保持 `input_mode: windows`。这条路径使用全部供应目标，直接调度，无需 TLE 或重新传播轨道。

本机可直接复用已执行 E0 的标准表导出，验证真实数据窗口接口：

```powershell
& $py -X utf8 smoke_test.py --config configs/smoke.yaml --input-mode windows --input-dir outputs/E0_seed42/standard_input --method baseline --out outputs/window_delivery_01
```

TLE 兼容路径会解析 Alpha-5、审核优先级和站址、冻结样本、生成并缓存几何窗口。更改 seed 时使用 `configs/seed17.yaml` / `configs/seed23.yaml`，或删除复制配置中的旧 `frozen_sample` 后设置新种子，不能把 seed42 冻结样本冒充新种子。每档数量分别由 `sample_counts` 控制。

## 3 号消费与评价

读取 `tasks_baseline.csv`，任务至少含 target_id、station_id、UTC 起止、duration_s、status、task_id。`metrics.json` 提供任务分、全部目标与可执行目标两个覆盖分母、分档覆盖及违例；`checks/` 是独立校验证据。重规划任务是 `tasks_baseline_replanned.csv`，对应 `fault_event.json`、`history_baseline.json` 和 `interrupted_baseline.csv`。

只读复查已完成运行：

```powershell
& $py -X utf8 smoke_test.py --check-only --source outputs/E0_seed42 --out outputs/recheck_delivery_01
```

报告在新目录 `recheck.json`，源文件前后哈希在 `source_hashes_before.json` / `source_hashes_after.json`。不要用旧 `prototype_v1` 的复查命令。

评价外部任务表（例子使用 E0 的权威目标/窗口）：

```powershell
& $py -X utf8 smoke_test.py --config configs/smoke.yaml --input-mode windows --input-dir outputs/E0_seed42/standard_input --evaluate outputs/E0_seed42/tasks_baseline.csv --out outputs/evaluate_delivery_01
```

故障重规划外部评价：

```powershell
& $py -X utf8 smoke_test.py --config configs/smoke.yaml --input-mode windows --input-dir outputs/E0_seed42/standard_input --evaluate outputs/E0_seed42/tasks_baseline_replanned.csv --initial-plan outputs/E0_seed42/tasks_baseline.csv --event outputs/E0_seed42/fault_event.json --interrupted outputs/E0_seed42/interrupted_baseline.csv --out outputs/evaluate_fault_delivery_01
```

评价方需独立保存初始计划，不能让被评价的规划器同时替换历史来源。评价重算实际时长与原始优先级得分，不采用任务自报 score。标准窗口模式只承诺窗口约束，不声称验证完整光学条件；TLE 模式附加真实逐秒仰角检查。

返回码：0=核心流程 PASS，2=非法输入或约束 FAIL，3=预算/缺输入/运行条件 BLOCKED。可选旧复评入口保持 BLOCKED，见 [说明](docs/optional_evaluator.md)，不影响本轮主校验器评价，也不能用主流程 PASS 代替旧复评状态。

## 回归与后续

```powershell
& $py -X utf8 -m pytest tests -q
& $py -X utf8 smoke_test.py --config configs/seed17.yaml --out outputs/seed17_delivery_01
& $py -X utf8 smoke_test.py --config configs/seed23.yaml --out outputs/seed23_delivery_01
```

实际实验见 [EXPERIMENTS.md](EXPERIMENTS.md)。该段为原基线轮交付背景。此后E3/E4、Demo和E5已完成，历史记录保留；当前稳定接入以 [STABLE_HANDOFF.md](docs/STABLE_HANDOFF.md) 和 [TEAM3_INTEGRATION.md](docs/TEAM3_INTEGRATION.md) 为准，不再将这些实验列作未开始。
