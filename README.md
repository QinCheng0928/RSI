# 种群式算法自进化框架

框架采用 **生成 → 测试 → 评估（含选择）→ 下一代生成**。每个生成名额只产生一次候选；源码有语法、接口、运行或提交问题时，保存原始输出和错误，继续交给后续阶段。本轮不会根据新发现的错误修复、重新生成或追加补测。评估完成后，下一代生成算子才能使用这些反馈。

当前问题为 `port_route_planning`，真实仿真接口、观测/动作结构和领域正确性检查保留 TODO。完整 `run` 在接口未接入时记录 `RUN_ABORTED`；配置检查、准备、独立生成和场景构建可单独使用。

## 目录

```text
evolution/
  generation/developer.py    # 一次生成；失败亲本修复，正常亲本随机交叉/变异
  testing/                  # 黑盒场景、importlib.util 加载、执行和精确缓存
  evaluation/               # 故障诊断、独立异常分析、裁决、偏好和选择
  framework.py              # 每个问题实现的 ProblemSpec
  llm.py                    # 统一 OpenAI SDK；load_dotenv 读取 .env
  runner.py                 # 固定代数、阶段编排、断点恢复
  storage.py                # 不可变制品、原子种群提交
problems/port_route_planning/
  problem.yaml              # 任务、场景空间、Rubric、模型和实验参数
  spec.py                   # 场景校验、确定性正确性检查、反馈脱敏
  template/algorithm.py.txt  # 本问题算法模板
  experiments/              # 同一问题的配置覆盖：cloud / no_cache
tests/                      # 框架测试，使用显式模型/仿真替身
requirements.txt            # 仅固定四个运行核心库版本
```

领域接口和独立问题目录的组织参考 [LACE](https://github.com/PJ-NTU/LACE)。执行服务放在测试模块，选择算子放在评估模块。

## 安装与启动

Python ≥ 3.9。从项目根目录运行，无需安装项目包：

```bash
python3.9 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m evolution validate --problem port_route_planning
.venv/bin/python -m evolution prepare --problem port_route_planning --run-id experiment_001
```

测试依赖单独安装：`pip install pytest==8.4.2`，然后执行 `python -m pytest -q`。

## 每代如何工作

`experiment` 当前设置：

```yaml
experiment:
  population_size: 10
  generations: 8
  seed: 42
```

第 0 代生成 10 个候选，全部进入测试和评估并保存为初始种群。随后执行 8 代，每代从上代选出的种群产生 10 个后代，父子代共 20 个候选进入同一测试套件和评估，再由选择算子保留 10 个。

生成阶段根据上一轮已经完成的评估结果分流。这里的亲本指上一轮选择算子保留的 P 个体；已淘汰的候选不会自动重新加入下一代。

每个需要修复的亲本各生成一个 `REPAIR` 后代，剩余后代名额只从不需要修复的亲本中随机抽取来源，再随机选择交叉或变异：

```yaml
genetics:
  crossover_probability: 0.5
  mutation_probability: 0.5
  elite_count: 1
```

- `REPAIR`：逐个处理上代保留的失败亲本，使用已记录的错误和评估反馈，每个亲本恰好生成一个修复后代。
- `CROSSOVER`：只从不需要修复的亲本中随机选两个不同个体，生成重组后代。
- `MUTATION`：只从不需要修复的亲本中随机选一个个体，生成变异后代。

交叉和变异的概率之和必须为 1，只用于非修复名额。`repair_probability` 已移除，修复数量由实际需要修复的亲本数决定，不设置固定修复配额或修复存活名额。`elite_count` 仍仅由评估选择使用。

例如 P=10，上一轮保留的 A、B 需要修复，则下一轮为 A、B 各生成一个修复后代，其余 8 个后代通过正常亲本的交叉或变异生成。只有一个正常亲本时，非修复名额使用变异，并在操作记录中注明；全部亲本都失败时，下一轮生成 P 个修复后代。

本轮新产生的错误只交给评估记录，不能反过来触发本轮的修复。修复后代仍然失败时，也必须完成本轮测试、评估和选择，若被保留，才在再下一轮继续修复。`offspring_manifest.json` 保存修复对象、可参与交叉/变异的亲本及实际算子数量。

每次模型输出均保留独立候选 ID。重复源码继续进入测试和评估，代码哈希用于原始运行缓存及选择多样性。输出格式错误、缺少 `algorithm.py` 或文件路径无法合法写入时，保存完整原始输出并记录生成错误；候选仍进入故障评估，不伪造运行轨迹。

评估先检查可运行性与硬约束。具备有效行为的候选进行 m 个独立首轮分析、依赖检验、交叉审查、证据核验、单一裁决及匿名换序偏好比较。失败者保存真实错误诊断，不参与行为质量比较。选择优先保留评估合格的可运行候选；数量不足时，从其余候选中按错误类别和多样性规则保留后续进化材料。全为失败候选时仍可进化，但不报告可靠行为质量或可部署算法。

偏好比较预算由 `population_size` 自动推导，无需在 YAML 中填写 `comparison_budget_per_generation`。最多有 `2P` 个父子候选，全部两两比较并交换顺序复评，调用上限为 `2P × (2P - 1)`。实际仅比较具备比较资格的 K 个候选，执行 `K × (K - 1)` 次调用，覆盖全部配对。例如 P=10 时上限为 380 次；若只有 10 个候选可比较，则执行 90 次。第 0 代只有 P 个候选，按实际人数计算。这些调用数只计偏好比较，不包含异常分析和裁决。每代的 `evaluation/comparison_summary.json` 保存自动上限、实际人数、配对数和调用数。

已删除同轮生成重试、失败槽位、生成准入审查及修复配额相关代码和配置。基础设施不可用仍显式中断；基础设施的有限请求重试不修改任何候选源码。

## 模型和密钥

项目根目录 `.env` 填写：

```dotenv
LLM_CLOUD_API_KEY_ENV=OPENAI_API_KEY
OPENAI_API_KEY=your-cloud-api-key
LLM_LOCAL_API_KEY_ENV=VLLM_API_KEY
VLLM_API_KEY=local-development
```

`OpenAIClient` 使用 `load_dotenv` 读取 `.env`，已有进程环境变量优先。`LLM_<PROFILE大写>_API_KEY_ENV` 指定该 profile 使用的密钥变量名，实际密钥保存在对应变量中。例如新增 `judge` 模型时，在 `.env` 配置 `LLM_JUDGE_API_KEY_ENV=JUDGE_API_KEY` 和 `JUDGE_API_KEY=...`。映射缺失或密钥为空时明确报错；密钥变量名和值均不进入问题配置和配置快照。

`problem.yaml` 的 `models` 只配置各端点的 `base_url`、`model` 和调用参数，不包含凭据字段；`agents` 选择各角色使用哪个模型配置。所有调用使用同一个 OpenAI SDK 客户端协议；本地模型由 vLLM 提供 OpenAI 兼容服务。例如在单独的 vLLM 环境启动：

```bash
vllm serve /path/to/model --served-model-name algorithm-model --host 127.0.0.1 --port 8000 --api-key local-development
```

默认角色使用 `local`。使用 `--experiment cloud` 可切换全部角色到 `cloud`；先填写云端实际模型名及密钥。`agents.analysts` 可指定一个共享 profile 或恰好 m 个 profile，每次分析均为无共享历史的独立会话。

模型输出按 Pydantic Schema 校验，默认使用 `json_object`；端点支持时可显式配置 `json_schema`（`strict: false`）。没有失败后自动切换协议的兼容分支。

## 独立生成、完整实验与恢复

```bash
python -m evolution generate --problem port_route_planning --run-id generation_001
python -m evolution build-suite --problem port_route_planning --run-id scenarios_001
# 完成实际仿真与领域契约接入后：
python -m evolution run --problem port_route_planning --run-id experiment_001
python -m evolution run --problem port_route_planning --experiment no_cache --run-id no_cache_001
```

独立生成保存全部候选，状态为 `GENERATED_UNTESTED`，不提交种群。完整实验只在第 0 代及后续 N 代全部完成后标记 `COMPLETED`。恢复时使用相同 run ID；配置、问题文件和框架源码均已冻结，改变流程或实验设置时使用新的 run ID。

## 中间产物

```text
runtime_data/<problem_id>/<run_id>/
  config/                       # 解析配置、问题和框架文件快照
  individuals/<id>/             # code/、源码清单、元数据、谱系、补丁
  generations/gen_0000/attempts/<UTC时间_ID>/
    strategy_plan.json          # 初始策略
    generation/slot_000/        # 单次输出、LLM 请求/响应/校验记录
    generation/offspring_manifest.json
    testing/                    # 场景、执行引用、覆盖和实际错误
    evaluation/                 # 正确性、故障、首稿、审查、证据、偏好、选择
    runtime_state.json
    memory_event.json
  populations/                  # 各代不可变种群及原子当前指针
  objects/                      # 内容寻址的原始运行、日志、证据和快照
  execution_cache/              # 精确执行键索引
  memory/                       # 已审查的事件
  events/                       # 缓存损坏、中断等事件
  standalone/                   # 独立生成和场景构建
  experiment_manifest.json      # 完成代数、个体状态、部署候选、中断原因
```

`attempts` 用于进程中断后的恢复记录，不是同轮修复循环。完整阶段可从已有产物恢复，不重新生成。模型原始响应、无法写入的文件提案和每个错误都会归档。

执行缓存绑定代码、场景、种子、环境、执行参数和版本指纹。相同代码的多个候选可引用同一个不可变原始运行，`RunUse.individual_id` 标明使用者，原始记录中的 ID 保留实际运行来源。生成错误记录明确标注 `GENERATION_ERROR`，不计为真实仿真调用，也不写入运行缓存。缓存损坏重新执行；非确定性模式统一重采样父子代。

## 接入问题与仿真 TODO

每个问题在 `problems/<problem_id>/` 中实现：

| 文件/方法 | 职责 |
|---|---|
| `problem.yaml` | 任务、场景、Rubric、模型和进化配置 |
| `spec.validate_scenario` | 根据真实参数范围校验场景 |
| `spec.check_correctness` | 根据真实运行结果检查硬约束 |
| `spec.sanitize_feedback` | 仅输出场景 ID 和脱敏行为症状 |
| `template/algorithm.py.txt` | 约定 `solve(observation, seed) -> dict` |
| `experiments/<name>.yaml` | 对默认配置递归合并字典，列表整体替换 |

未来仿真适配器配置到 `execution.simulator`，在执行进程中使用：

```python
from evolution.testing import load_algorithm

solve = load_algorithm(code_directory / "algorithm.py")
action = solve(observation, scenario.seed)
# TODO: 送入真实仿真器，记录实际行为与错误。
```

加载器使用 `importlib.util.spec_from_file_location → module_from_spec → exec_module`。适配器返回 `INVALID` 表示算法语法、接口、运行或超时错误，也可抛出包含真实错误和类别的 `AlgorithmExecutionError`；执行器记录后继续。返回 `EXECUTION_BLOCKED` 表示操作被执行环境阻止，这类候选同样进入评估。服务故障抛 `InfrastructureError`。适配器不得根据运行错误在本轮调用生成模型修改代码。

测试使用显式替身验证框架，不代表真实模型、仿真或执行隔离已接入。
