# Population-Based Algorithm Evolution

<p align="center">
  <a href="../README.md">English</a>
  &nbsp;·&nbsp;
  <strong>简体中文</strong>
</p>

一个通过大语言模型生成、测试和迭代优化算法的研究框架。模型根据任务描述与实际交互证据判断算法表现，框架负责管理候选代码、测试场景、评估反馈和种群演化。

项目面向需要与外部环境交互、缺少明确数值评价函数的算法优化任务。每个问题作为独立的问题包，定义任务、环境交互方式和评价约定。

## 主要功能

- 基于种群进行算法搜索，支持修复、交叉和变异算子。
- 区分执行或约束失败与行为表现较弱，失败诊断用于下一代修复。
- 使用公共场景集测试父代和子代，结合多分析员评审及成对比较进行选择。
- 保存候选源码、模型交互、执行证据和评估记录，支持从已提交的代际结果恢复运行。
- 通过统一接口接入不同问题，由问题自己的仿真器负责实际交互与候选代码隔离。

## 项目结构

```text
evolution/
  generation/       # 代码生成与演化算子
  testing/          # 场景生成、执行、重试与记录
  evaluation/       # 诊断、行为分析、比较与选择
  framework.py      # 问题与仿真器的统一接口
  runner.py         # 代际流程调度与恢复
  storage.py        # 实验记录与种群提交
problems/
  <problem_id>/     # 一个独立的问题包
requirements.txt
```

每个问题在自己的目录中维护 `problem.yaml`、`spec.py`、`simulator.py` 和算法模板。顶层 `problem_id` 是唯一的问题标识，`task` 描述目标、输入输出和约束。

## 安装

使用 Linux 和 Python 3.8 或兼容的更高版本，从仓库根目录执行：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

框架使用 Linux 文件锁。各问题的适配器还需要提供对应的环境依赖、候选代码隔离和就绪检查，具体要求由接入的环境决定。

## 模型配置

在对应问题的 `problem.yaml` 中设置：

- `models`：模型服务地址、模型名称、超时和请求参数。
- `agents`：生成、测试、分析和评判等角色使用的模型配置。

框架通过 OpenAI 兼容的 Chat Completions 接口调用模型，可连接远程服务或本地 vLLM。模型服务需要支持配置中指定的 `json_object` 或 `json_schema` 输出模式。

在仓库根目录创建 `.env`：

```dotenv
LLM_CLOUD_API_KEY_ENV=OPENAI_API_KEY
OPENAI_API_KEY=replace-with-your-cloud-key

LLM_LOCAL_API_KEY_ENV=VLLM_API_KEY
VLLM_API_KEY=local-development
```

`LLM_CLOUD_API_KEY_ENV` 和 `LLM_LOCAL_API_KEY_ENV` 指定密钥所在的环境变量名称。只配置实际使用的模型角色所需的凭据即可；进程中已有的环境变量优先于 `.env`。使用本地服务时，密钥应与服务端设置一致。

`.env` 已被 Git 忽略。密钥应保存在环境变量或根目录 `.env` 中，不要放入会被实验归档的 `problem.yaml`。

## 运行优化

先完成模型配置和所选问题的环境准备。将 `your_problem` 替换为 `problems/` 下已有的问题目录名称：

```bash
source .venv/bin/activate
python -m evolution run --problem your_problem --run-id experiment_001
```

实验规模在问题配置中设置。下面是一个小规模示例，实际运行以对应的 `problem.yaml` 为准：

```yaml
experiment:
  population_size: 4
  generations: 2
  seed: 42

genetics:
  crossover_probability: 0.5
  mutation_probability: 0.5
  elite_count: 1
```

`population_size` 表示每代保留的算法数量。`generations: 2` 表示初始代 `gen_0000` 之后再迭代两代；后续每代生成 P 个子代，与 P 个父代共同测试、评估，再保留 P 个候选。`elite_count` 必须小于 P，交叉与变异概率之和必须为 1。

其他命令使用相同的 `--problem` 和 `--run-id` 参数：

| 命令 | 用途 |
| --- | --- |
| `python -m evolution prepare` | 保存配置与源码快照，不调用模型或检查仿真服务 |
| `python -m evolution generate` | 仅生成候选代码，不执行测试 |
| `python -m evolution build-suite` | 仅构建测试场景 |
| `python -m evolution run` | 执行完整优化流程 |

`generate` 和 `build-suite` 需要可用的模型服务。请显式传入 `--problem`，选择要运行的问题包。

保持配置与源码不变，使用相同 `run-id` 可从已提交的代际结果恢复。修改问题配置、问题实现或框架源码后，应使用新的 `run-id`。

## 演化与评估规则

每个生成名额产出一个候选，当前代发现的失败不会触发同代修复重跑。上一代被保留的失败候选各获得一个修复子代；其余名额在正常父代之间执行交叉或变异。只有一个正常父代时使用变异。

执行失败、输出不合规、违反硬约束或缺少有效证据的候选会收到失败诊断。通过检查的候选进入行为分析、交叉评审和成对偏好比较。基础设施故障由有限重试处理，重试耗尽后中止运行，不作为算法表现评分。

选择优先保留通过检查的候选。有效候选不足 P 个时，剩余位置可保留失败候选，供下一代修复。每对有效候选按正反顺序分别比较一次；评估请求数量随有效候选数量近似按平方增长，实验规模应结合模型调用成本和仿真耗时设置。

最终输出是当前测试集支持的推荐结果。框架保留平局、不可比和偏好环等情况，不保证找到唯一或全局最优算法。

## 问题包结构

每个问题将配置、任务约定、环境适配器和算法模板放在同一个目录：

```text
problems/<problem_id>/
  problem.yaml                  # 组件引用与实验配置
  spec.py                       # 场景合法性与正确性规则
  simulator.py                  # 环境交互与隔离执行
  template/
    algorithm.py.txt            # 算法接口与初始实现
  sim_config/                   # 可选的环境配置或固定资源
  experiments/                  # 可选的 YAML 配置覆盖文件
```

前四个文件构成标准的问题包布局。其他模块和资源按需添加，属于具体问题的内容应保留在问题目录内。框架通过配置加载组件，不直接依赖某个具体环境。

### 配置：`problem.yaml`

配置同时描述任务含义和运行实验所需的设置：

| 配置项 | 职责 |
| --- | --- |
| `problem_id` | 唯一的问题标识，也用于构造运行记录目录 |
| `spec` | `ProblemSpec` 实例的导入引用，例如 `problems.your_problem.spec:SPEC` |
| `template` | 相对于问题目录的算法模板路径 |
| `storage_root` | 实验记录的根目录 |
| `task` | 目标、输入、输出、硬约束和允许修改的代码文件 |
| `scenario_space` | 场景类型、参数范围与合法性规则 |
| `core_scenarios` | 预定义的基础场景，可以为空 |
| `rubric` | 行为评价原则及证据冲突时的权衡指导 |
| `experiment` | 种群规模、演化轮数与框架随机种子 |
| `genetics` | 交叉和变异概率、精英数量 |
| `evaluation` | 独立初始分析员的数量 |
| `models` / `agents` | 模型服务配置及各角色使用的模型 |
| `execution` | 仿真器引用、版本、资源限制、重试和环境设置 |

`task.goal` 描述期望行为；`task.inputs` 和 `task.outputs` 定义算法的观测与动作约定；`task.hard_constraints` 列出必须满足的规则；`task.allowed_changes` 限制生成阶段能够提交的文件。不再设置单独的任务标识。

`execution.environment` 保存适配器专用配置，各测试场景之间变化的输入应放入 `Scenario.inputs`。`simulator_version`、`harness_hash`、`observation_schema_version` 和 `external_inputs_hash` 标识执行条件，由适配器在 `check_readiness()` 中校验相关版本和指纹。

可选的 `experiments/<name>.yaml` 通过 `--experiment <name>` 覆盖基础配置。字典递归合并，列表整体替换对应的基础列表。

### 场景与正确性：`spec.py`

每个场景包含标识、问题专用输入、随机种子和设计目的：

```yaml
scenario_id: baseline_001
inputs:
  case: baseline
seed: 42
purpose: Baseline coverage
```

上述 `inputs` 仅用于展示结构，具体字段由问题自己定义和校验。场景内容哈希取决于输入与种子，场景标识和目的属于说明信息。

实现 [ProblemSpec 接口](../evolution/framework.py)：

| 方法 | 职责 |
| --- | --- |
| `validate_scenario(scenario)` | 拒绝不合法输入、不支持的参数和不可行的初始条件 |
| `check_correctness(scenario, result)` | 根据实际执行证据检查强制约束，返回 `Correctness` |
| `sanitize_feedback(confirmed)` | 向后续场景生成仅暴露场景标识与行为症状 |

正确性检查回答候选是否满足强制要求，行为评价判断合格候选实现目标的效果，两者分开处理。

测试器合并预定义、历史、回归及新提出的场景。输入与种子相同的场景会去重；同一场景标识不能对应不同内容。父代与子代使用相同的当代场景集。

### 环境与执行：`simulator.py`

实现 [Simulator 接口](../evolution/framework.py)：

| 成员 | 职责 |
| --- | --- |
| `sandboxed` | 声明适配器是否提供真实的候选执行隔离 |
| `check_readiness(config)` | 检查环境可用性、依赖、版本、指纹及隔离条件 |
| `run(code_directory, scenario, config)` | 执行一个候选在一个场景中的测试，以 `SimulatorResult` 返回真实观测和错误 |
| `testing_batch(config)` | 可选的上下文管理器，用于在一代全部测试之间复用资源 |

适配器在执行边界内加载生成算法，获取观测、调用决策函数、应用动作并记录行为。对按步交互的环境，应逐步保存要求的观测、动作和结果，并包含初始与终止状态。资源限制和依赖约束必须由实际执行环境落实。

`SimulatorResult` 包含执行状态、决策、轨迹、上下文、标准输出、标准错误和可选的失败类别。算法失败返回 `INVALID` 或抛出 `AlgorithmExecutionError`；环境或服务故障使用 `InfrastructureError`。适配器不在执行过程中修复或重新生成候选。

executor 遍历候选和场景，管理基础设施重试、执行记录与覆盖检查。一次 `executor.run()` 对应一代的测试批次；如果适配器提供 `testing_batch()`，其上下文包围整个批次，并在正常结束或异常退出时清理资源。适配器决定在该范围内如何复用或重建资源。

外部环境可能存在非确定性：状态差异、推进时序和异步通信可能使相同代码、场景输入及种子产生不同观测。因此，每代在当代公共场景集上重新测试父代与子代，使用本轮实际执行证据评价，不将历史轨迹复用为本次结果。

### 算法约定：`template/algorithm.py.txt`

模板定义同步入口 `solve(observation, seed)`、观测字段、动作结构、可用依赖及其他限制，也可以提供供生成算子替换的初始实现。

观测在候选算法外部获取，再通过参数传入。生成文件受 `task.allowed_changes` 限制，其中必须包含 `algorithm.py`。环境初始化、正确性检查和评价器内部实现不属于生成代码。

## 查看结果

运行记录保存到 `runtime_data/<problem_id>/<run_id>/`：

```text
config/                    # 配置与源码快照索引
individuals/<id>/code/     # 各候选的 algorithm.py
individuals/<id>/          # 元数据、父代关系及修改记录
generations/               # 每代的生成、测试与评估记录
populations/               # 已提交的种群与恢复指针
memory/                    # 供后续代使用的评审反馈
objects/                   # 执行证据、日志与快照等内容对象
final/best.json            # 最终推荐结果与依据
final/algorithm.py         # 存在合格候选时导出的推荐代码
experiment_manifest.json   # 整体状态、进度、可用候选与中止原因
```

模型请求和回复、失败诊断、测试轨迹及比较记录都保存在实验目录中。`COMPLETED` 表示流程已完成，不保证存在可用算法；若最终没有合格候选，`final/best.json` 会记录 `NO_VALID_CANDIDATE`，不会导出推荐代码。

框架模块划分参考 [LACE](https://github.com/PJ-NTU/LACE)。
