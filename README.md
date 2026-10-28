# bd-fmb-observation-planner

自主观测任务规划。项目提供贪心基线、改进算法、站点故障后的重规划、原生无故障重排保护、独立校验及离线 Demo 生成工具。

Git 仓库包含源码、测试、合成示例和接入文档；真实比赛数据与完整运行产物保存在本地。无真实数据时也可以运行下面的合成流程。

## 快速开始（Windows / Python 3.11）

在 PowerShell 执行：

```powershell
git clone https://github.com/zindigo321/bd-fmb-observation-planner.git
Set-Location bd-fmb-observation-planner

conda create -n bd-fmb python=3.11 -y
conda activate bd-fmb
python -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw '依赖安装失败' }

python -X utf8 scripts/team3_run.py synthetic --out outputs/synthetic_01
if ($LASTEXITCODE -ne 0) { throw '查看控制台和 workflow.json' }
```

已有 `bd-fmb` 环境时，从激活环境开始。源码包解压后同样可从包根运行，不需要 `.git`。每次必须使用新的输出目录，例如下次改成 `outputs/synthetic_02`。

该命令依次运行基线规划、初始/故障独立评价和只读复查。合成例预期初始10分、故障后3分，两者均零违例；它验证接口和故障处理，不代表真实算法收益。结果位于 `outputs/synthetic_01`，总回执为 `workflow.json`。

## 运行测试与协作

```powershell
python -m pip install -r requirements-dev.txt
if ($LASTEXITCODE -ne 0) { throw '开发依赖安装失败' }
New-Item -ItemType Directory -Path outputs -Force | Out-Null
python -X utf8 -m pytest tests -q --basetemp=outputs/pytest_01
```

测试目录也应每次使用新名称。GitHub 工作流提供 `Windows Python 3.11` 检查，首次云端运行状态以 [Actions 页面](https://github.com/zindigo321/bd-fmb-observation-planner/actions)为准。协作、PR 和格式约定见 [CONTRIBUTING.md](CONTRIBUTING.md)。干净环境安装是否已通过，应以云端安装步骤或单独安装回执为证，不能由现有环境运行结果推断。

## 项目结构

| 目录/入口 | 用途 |
| --- | --- |
| `planner/` | 调度、重规划、独立校验、窗口适配及安全读写 |
| `scripts/` | 团队接入、数据试读、受控实验和 Demo 生成 |
| `smoke_test.py` | 规划、独立评价及只读复查统一入口 |
| `configs/` | 合成及固定种子实验配置 |
| `examples/synthetic/` | 可公开运行的合成输入 |
| `tests/` | 接口、故障历史、路径和写入安全回归 |
| `demo/` | 离线演示模板和说明 |
| `docs/` | 接入契约、交接与实验说明 |

## 队友接入与实验结果

- [3号接入说明](docs/TEAM3_INTEGRATION.md)：从解压、运行到独立评价。
- [1号数据试读](docs/TEAM1_DATA_INTAKE.md)：标准窗口四文件及来源检查。
- [输入输出契约](docs/io_contract.md)：UTC、字段、故障边界和返回码。
- [原生无故障流程](docs/NO_FAULT_NATIVE.md)：冻结 source 包、严格选择、真实历史评价与零求解重查。
- [稳定交接](docs/STABLE_HANDOFF.md)：源码包与仅本地转交的参考包。
- [离线 Demo](demo/README.md)与[演示讲稿](docs/DEMO_SCRIPT.md)。
- [E5受控对照](docs/PATH_SAFETY_E5_HANDOFF.md)、[多种子与较晚故障实验](docs/IMPROVEMENT_DEMO_HANDOFF.md)、[原子写入修复](docs/ATOMIC_WRITE_FIX_HANDOFF.md)。
- [基线交接](BASELINE_HANDOFF.md)、[实验记录](EXPERIMENTS.md)、[进度记录](PROGRESS.md)、[团队待确认项](docs/team_questions.md)。

截至2026年10月27日，本机实验及独立审计已完成至第十三轮。原生 `no-fault` 在完整冻结初始上重建 completed/locked/new_success，验证候选合法后只在 `J=(权威整数分,唯一覆盖)` 严格字典序改善时接纳；低值和平局保留原任务字段与行顺序。主分不下降，主分提高时覆盖仍可能下降。该保护适用于新增原生入口，不表示历史故障/legacy 入口自动获得相同策略。

R11开发快照回归为280项通过（149原有＋103原生接口＋28预算回归）。R12完成同源同日60/180嵌套规模的8个原生键、12次求解、24份外检和12份原始TLE几何；R13固定60目标，在2026-08-05、08-06两个模型日完成同样计数。R12的selected方法差为3.2/0/35.2/0分，R13为5.2/0/7.2/0分，均是同source内improved减baseline，不作独立样本或普遍因果解释。第十四轮十站对照仅为计划，尚未执行。

本次提交候选另校正父端收尾预算检查：子命令成功后，输入保护快照与最终状态处理也必须消耗同一父deadline，超期不能返回整工作流PASS。这是提交前新增差异，不是R11–R13已执行源码。当前提交的完整回归、合成接入和GitHub CI应按PR中的本次实测记录判断，不能以历史280项或真实实验代替。

程序实现沿用R11快照来源树 `54766909c8f539c8af3cfe733378da7e3aafc347206d904f7b5dbaed6f5a3354` 并包含上述提交前校正；该开发树摘要不是Git提交SHA，实际提交以PR为准。历史结果、保留失败和范围见[当前状态](docs/CURRENT_STATUS.md)、[实验记录](EXPERIMENTS.md)。真实数据与机器证据保留本地，不随仓库发布。

## 本机真实 TLE 实验

以下命令仅适用于已有忽略目录 `prototype_v1/data_raw` 与原冻结样本的本机工程。新克隆应先运行合成示例；旧 TLE 全量移机未验证。

```powershell
# 在本机项目根、已激活的既有环境中执行。
$py = 'python'
& $py -X utf8 smoke_test.py --config configs/smoke.yaml --method baseline --out outputs/baseline_01
if ($LASTEXITCODE -ne 0) { throw '规划未通过' }
& $py -X utf8 smoke_test.py --check-only --source outputs/baseline_01 --out outputs/recheck_01
if ($LASTEXITCODE -ne 0) { throw '复查未通过' }
```

不修改原始数据和封存结果。E5复核须使用其专用 `scripts/run_replan_e5.py --check-only`，见对应交接说明。`requirements-lock.txt` 为本机原有完整环境快照，保持忽略，不作为公共依赖文件。

## 验证范围

每目标一次成功、中断零分、120/240秒离散得分、海拔近似椭球高是当前测试口径。几何仰角不代表完整光学可观测；标准窗口接入只检查供应窗口内的调度约束，不认证夜间、受光、天气等声明。R12增加同源同日的60/180相关规模，R13增加同一60目标的跨日/龄期敏感性；仍为3站、24小时模型。十站完整六天、全10000目标、独立新来源、完整光学设备、正式评分和异机接收发布未验证。真实运行包仅在本机新路径完成迁移及零求解重查，不等于异机/设备现场验收。正式规则和两套评分差异待明确。
