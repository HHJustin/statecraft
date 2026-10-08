# statecraft · 工程实施规划（PLAN）

> 本规划源自《自建Agent项目统一架构设计与落地路线图.md》（下称「路线图」），把其中的 8 个设计决策 + 10 阶段落地路线，翻译成**可直接编码**的工程方案。项目代号 **statecraft**。

---

## 0. 目标与范围

**目标**：实现一个「状态机驱动、审核兜底、可观测、自带记忆」的最小多 Agent 框架，且**核心零依赖、离线可跑、离线可测**。

**本期范围**：路线图阶段 1–5 的核心能力（状态机、工具契约+权限、Provider 工厂、记忆层、离线测试）。
**暂缓**：阶段 6–10（事件总线+Outbox、评估体系、安全加固、可观测、降级瘦身），见第 5 节里程碑表。

## 1. 技术选型

| 项 | 选择 | 理由（呼应路线图第 9 节） |
|---|---|---|
| 语言 | Python ≥3.10 | asyncio 原生、类型标注 |
| 核心依赖 | **零**（纯 stdlib） | 学习/练习价值最大化；起步够用 |
| LLM 接入 | OpenAI 兼容接口（`base_url` 可切 DeepSeek/Qwen/GLM/Ollama） | 一个 base_url 换所有厂商；httpx 按需 optional |
| 测试 | unittest（stdlib）+ MockLLM | 照抄 Mem0：**测试离线、秒级、不依赖真实 LLM** |
| 存储 | 记忆先用内存版（接口即契约），事件总线先用 asyncio.Queue | 起步 SQLite/内存，上规模换 Postgres/Redis |
| 并发模型 | asyncio 单进程 | 阶段 1–5 不需要分布式；阶段 6 再引事件总线 |

## 2. 目录结构

```
statecraft/
├── PLAN.md                  # 本文档
├── README.md                # 快速上手
├── pyproject.toml           # 核心零依赖，httpx 为 optional
├── statecraft/
│   ├── __init__.py
│   ├── config.py            # AgentCoreConfig（超时/退避/注入条数/auto_confirm）
│   ├── core/                # —— 治理层（源自 Edict）——
│   │   ├── state_machine.py # 决策一/四：白名单 + 升级路径 + 高风险二次确认 + Task
│   │   ├── tools.py         # 决策八：工具注册表 + AGENT_POLICY 权限白名单 + 审计日志
│   │   ├── runner.py        # AgentRunner：prompt 分层(GLOBAL→角色) + 工具循环
│   │   └── orchestrator.py  # 编排循环：记忆注入 + 超时重试 + 停滞升级 + 回滚钩子
│   ├── memory/              # —— 记忆层（源自 Mem0）——
│   │   ├── base.py          # 决策五：Memory 5 方法协议 + MemoryItem(含 score_details)
│   │   └── local.py         # 本地实现：哈希去重 + 相似度打分(阈值前置) + history 版本化
│   ├── providers/           # —— Provider 底座（源自 Mem0）——
│   │   ├── base.py          # LLMBase 协议 + LLMReply/ToolCall
│   │   ├── factory.py       # importlib 工厂 + register_provider + 配置三态归一化
│   │   ├── mock.py          # MockLLM：脚本化/可注入 handler，离线测试与 demo 用
│   │   └── openai_compat.py # OpenAI 兼容 provider（httpx 优先，urllib 兜底）
│   └── agents/              # 三层 prompt：GLOBAL.md → 角色文件
│       ├── GLOBAL.md        # 全局规范（上报义务/输出纪律/注入防御）
│       ├── planner.md critic.md executor.md
├── tests/                   # 全部离线，mock LLM
│   ├── test_state_machine.py test_tools.py test_memory.py test_orchestrator.py
└── demo.py                  # 最小闭环演示
```

## 3. 核心模块接口（先定契约，再写实现）

### 3.1 状态机（core/state_machine.py）
```python
STATE_TRANSITIONS: dict[str, set[str]]        # 白名单：只允许单向递进
ESCALATION_PATH = {"Executing": "Reviewing", "Reviewing": "Planning", "Planning": "Created"}
STATE_AGENT_MAP = {"Planning": "planner", "Reviewing": "critic", "Executing": "executor"}
HIGH_RISK_TRANSITIONS = {("Executing", "Done"), ("Reviewing", "Cancelled")}

class TransitionDenied(Exception)   # 白名单外流转
class TaskStalled(Exception)        # 超时重试耗尽 → 交给升级
class Task                          # snapshot()/rollback()/flow_log/progress_log/pending_target
def transition(task, to, *, confirm=False)   # 高风险未确认 → 先入 PendingConfirm
def approve(task) / reject(task)             # 二次确认
def escalate(task)                           # 沿升级路径退回，到顶 → Blocked
```

### 3.2 工具契约（core/tools.py）
```python
AGENT_POLICY: dict[str, {"role", "tools"}]   # 命令级权限白名单（源自 Edict）
check_permission(agent_name, role, tool)     # 越权 → PermissionDenied + AUDIT_LOG
@dataclass Tool(name, description, parameters, handler)
ToolRegistry.register/get/schemas(names)     # schemas 只暴露白名单内的工具给 LLM
内置工具：report_progress(text) / request_block(reason)
# 注意：update_state 不对 Agent 开放——改状态是编排器专属，这正是「受控接口」的体现
```

### 3.3 记忆层（memory/base.py + local.py）
```python
class MemoryBase(ABC):
    add(text, scope="shared", task_id=None, metadata=None) -> str
    search(query, limit=5, explain=False) -> list[MemoryItem]   # 阈值前置，explain 带 score_details
    update(memory_id, text) / delete(memory_id) / history(memory_id)
```
本地实现要点（照抄 Mem0 精髓）：MD5 确定性去重、cosine 相似度打分（中文 bigram + 英文词）、低于阈值直接丢弃、软删除、history 只增不改。

### 3.4 Provider 底座（providers/）
```python
class LLMBase(ABC):
    async def chat(self, messages, tools=None) -> LLMReply
@dataclass LLMReply(content: str, tool_calls: list[ToolCall])
LLMFactory.create(provider, config=None)     # 配置三态归一化：None/dict/实例
LLMFactory.register_provider(name, class_path)   # 开放第三方注册
```
内置 provider：`mock`（脚本化输出 + 可注入 handler）、`openai_compat`（httpx 优先，未装降级 urllib）。

### 3.5 AgentRunner 与编排器（core/runner.py + orchestrator.py）
```python
class Agent:
    def __init__(self, name, llm, role=None, system_prompt=None)
    async def run(self, task, context_text) -> str
        # 系统提示 = GLOBAL.md + 角色 md，头部加 [role:name] 标记
        # 工具循环：LLM 回 tool_calls → 逐个权限校验后执行 → 结果回填 → 直到最终文本
class Orchestrator:
    def create_task(description) -> Task
    async def run(task) -> Task
        # 循环：查状态 → 唤醒 STATE_AGENT_MAP 对应 Agent
        #   → memory.search 注入上下文 → snapshot() → run_with_retry(超时 wait_for + 退避)
        #   → TaskStalled → escalate() → 记忆沉淀 → decide_next → transition
        #   → Reviewing 打回 → Planning（副作用 undo 钩子留给阶段 6）
        #   → Executing 产出无 DONE 标记 → 计次重跑 → 超 Blocked
        #   → PendingConfirm：auto_confirm 自动过，否则等待人工
```

## 4. 关键设计决策落地对照

| 路线图决策 | 落地点 |
|---|---|
| 一 状态机白名单 | `state_machine.py` 全量白名单表 + `TransitionDenied` |
| 二 审核必经关卡 | `STATE_AGENT_MAP` 强制 Reviewing→critic；打回/兜底逻辑在编排器 |
| 三 Provider 工厂 | `providers/factory.py`（importlib + register_provider + 三态归一化） |
| 四 调度可靠性三件套 | `run_with_retry`（wait_for 超时+退避）、`ESCALATION_PATH`、`Task.snapshot/rollback` |
| 五 混合检索+可解释 | `memory/local.py` 打分 + `score_details`；sparse/向量检索留给接口后的实现 |
| 六 入库链路 | 本期为对话事实入库（哈希去重）；文档入库流水线（父子切片）留阶段 6+ |
| 七 可观测+降级 | flow_log/progress_log/AUDIT_LOG；httpx 缺失降级 urllib |
| 八 安全与约束 | `AGENT_POLICY` 权限白名单 + `HIGH_RISK_TRANSITIONS` 二次确认 + GLOBAL.md 注入防御条款 |

## 5. 里程碑（对应路线图 10 阶段）

| # | 阶段 | 交付物 | 验收标准 | 状态 |
|---|---|---|---|---|
| 1 | 最小闭环 | state_machine + runner + orchestrator + mock | demo 全绿：任务 Created→…→Done | 本期 |
| 2 | 工具契约 | tools.py + AGENT_POLICY + 审计 | 越权调用被拒且有审计记录 | 本期 |
| 3 | Provider 底座 | factory + mock + openai_compat | 换 provider 只改 config 字符串 | 本期 |
| 4 | 记忆层 | memory 5 方法 + local 实现 | 去重/打分/history 测试通过 | 本期 |
| 5 | 离线测试 | tests/ 四个套件 + demo + README | `python -m unittest` 全绿、秒级 | 本期 |
| 6 | 可靠性 | 事件总线 + Outbox + DLQ + 工具 undo/Saga | 崩溃自恢复测试；副作用可撤销 | ✅ 进程内版 + SagaLog 逆序补偿（打回/取消触发，审计留痕）；分布式总线暂缓 |
| 7 | 可观测 | JSONL 会话日志 + 活动流 + 简单看板 | 全链路可回放 | ✅ JSONL+活动流+审计+零依赖 HTML 看板（dashboard.py） |
| 8 | 降级瘦身 | 依赖分层 optional + 哨兵降级 | 缺依赖时核心链路照常跑 | 本期部分达成 |
| 9 | 评估体系 | 黄金集 + L1/L2/L3 评测脚本 | 一键跑分回归 | ✅ 轻量版：格式+跑分器+种子集，离线可回归 |
| 10 | 安全加固 | 注入检测 + 委派防死锁 + 原子锁 | 对抗样本测试通过 | ✅ 注入检测 + 委派双闸门（深度/成环）；原子锁待多进程场景 |

## 6. 编码与测试约定

1. **契约优先**：先写 ABC/协议与 dataclass，再写实现（依赖倒置，治理层不 import 具体实现）。
2. **离线可测**：所有测试不触网——LLM 用 MockLLM（脚本/可注入 handler），记忆用内存版。
3. **状态只经白名单**：任何地方改状态必须走 `transition()`，禁止直接 `task.state = ...`（`_set` 仅供内部与测试造景）。
4. **工具先鉴权**：LLM 返回的 tool_calls 一律先 `check_permission` 再执行，拒绝也回填给 LLM（让它知道边界）。
5. **降级不崩**：可选依赖（httpx）缺失走兜底路径并 logger.warning，不抛异常中断主流程。
6. **中文注释 + 关键处标注来源**：`（源自 Edict 决策X）`，方便回溯路线图。
