# statecraft

> **Statecraft** = state（状态机）+ statecraft（治国之术）——用「三省六部」式的治理纪律，管住多 Agent 系统的流程、权限与记忆。

状态机治理 + 可插拔记忆的最小多 Agent 框架。设计蓝图见 [PLAN.md](PLAN.md)，
来源是《自建Agent项目统一架构设计与落地路线图.md》（借鉴 Edict / Mem0）。

## 特性

- **状态机白名单**：状态流转必须命中 `STATE_TRANSITIONS`，白名单外直接拒绝
- **审核必经关卡**：Planning → Reviewing → Executing，critic 可打回
- **高风险二次确认**：完结/取消先入 `PendingConfirm`，需 approve/reject
- **调度可靠性**：超时重试（时钟）+ 停滞升级（逐级退回，到顶 Blocked）+ 快照回滚
- **事件驱动**：Outbox → EventBus → 订阅者；retryable 错误重投，超限/不可重试进 DLQ（P6）
- **可观测**：JSONL 会话日志 + 活动流 + 审计日志（P7）
- **黄金集评估**：L1 检索（Recall@K/MRR）+ L2 审核（混淆矩阵/F1）+ L3 端到端，一键离线跑分（P9）
- **注入检测**：轻量正则扫描，命中后隔离包装为不可信数据（P10 轻量版）
- **命令级权限白名单**：Agent 只能调 `AGENT_POLICY` 内的工具，越权拒绝 + 审计日志
- **记忆层 5 方法**：add/search/update/delete/history，MD5 去重 + 阈值前置打分 + 版本化
- **意图识别 / 分拣节点**：`create_task` 先分拣闲聊与任务（源自 Edict「太子分拣」），
  闲聊直通 Done 不进流水线；规则分拣零成本，`TriageBase` 协议可换 LLM 分拣；
  保守策略——拿不准按任务处理（漏放代价 < 误杀代价）
- **混合检索记忆**：`HybridMemory` = dense 向量 + 词面双路打分（`EmbeddingBase` 协议 +
  离线 `HashEmbedding`，真实 embedding API 即插即用）；`explain` 同时给出两路分值；
  黄金集实测改述样本 Recall 0.600 → 1.000
- **Provider 工厂**：换 LLM / 记忆后端都只改配置字符串（`LLMFactory` / `MemoryFactory`，
  支持注册第三方）
- **委派 + 防死锁**：任务可委派子任务（`delegate_subtask` 工具，planner 专属）；
  `DelegationTracker` 双闸门——深度超限 / 委派给祖先成环，直接拒绝（P10）
- **Saga 补偿**：Tool 声明 `undo` 即自动记账 `SagaLog`；打回 / 取消时逆序补偿，
  尽力而为 + 审计留痕（Blocked 保留现场不补偿）（P6）
- **静态看板**：Task + 会话日志 → 单文件 HTML（任务卡片 / 流转时间线 / 告警与补偿），
  零依赖浏览器直开（P7）
- **官方示例：codedoc** —— 用框架构建的代码注释生成与审查多 Agent 流水线：
  planner 规划 / writer 写入（Saga 可回滚）/ critic 审核，坏注释打回时自动撤销；
  **RAG 增强**——术语表与注释规范入混合记忆，writer 写注释前先检索规范注入上下文
- **核心零依赖，测试全离线**（MockLLM 脚本化，74 个测试秒级跑完）

## 快速开始

```bash
# 跑测试（离线）
python -m unittest discover -s tests -v

# 最小闭环演示 / 事件驱动演示 / 委派+补偿+看板演示 / 代码注释 Agent
python demo.py
python demo_events.py
python demo_dashboard.py   # 生成 dashboard_demo.html
PYTHONPATH=. python examples/codedoc/codedoc.py

# 黄金集一键跑分（L1/L2/L3 离线评估）
python -m statecraft.eval.runner
```

## 换真实 LLM

```python
from statecraft.config import AgentCoreConfig
config = AgentCoreConfig(
    llm_provider="openai_compat",
    llm_config={"base_url": "https://api.deepseek.com/v1",
                "api_key": "sk-...", "model": "deepseek-chat"},
)
```

核心无第三方依赖；装了 httpx 会走异步客户端，没装自动降级 urllib。

## 用法骨架

```python
import asyncio
from statecraft.config import AgentCoreConfig
from statecraft.core.orchestrator import Orchestrator
from statecraft.core.runner import Agent
from statecraft.memory.local import InMemoryMemory
from statecraft.providers.factory import LLMFactory

llm = LLMFactory.create("mock", {"script": {"planner": ["计划"], "critic": ["审核通过"], "executor": ["DONE"]}})
agents = {n: Agent(n, llm) for n in ("planner", "critic", "executor")}
orch = Orchestrator(agents, InMemoryMemory(), AgentCoreConfig())
task = asyncio.run(orch.run(orch.create_task("你的任务")))
print(task.state, task.flow_log)
```

## 目录结构

```
statecraft/
├── core/          # 治理层：状态机 / 工具权限 / AgentRunner / 编排器
│                  #         事件总线+Outbox+DLQ / Saga补偿 / 委派防死锁
│                  #         会话日志 / 注入检测
├── memory/        # 记忆层：5 方法协议 + 内存实现 + 混合检索（dense+词面）
│                  #         Embedding 协议 + 哈希伪向量 + 记忆后端工厂
├── providers/     # Provider 底座：工厂 + mock + openai 兼容
├── eval/          # 黄金集评估：L1/L2/L3 跑分器 + 种子样本（JSONL）
├── dashboard.py   # 零依赖静态 HTML 看板
├── agents/        # 三层 prompt：GLOBAL.md → 角色文件
└── tests/         # 离线测试
```

## 路线图后续（未实现）

分布式事件总线（Redis Streams / NATS）——当前 EventBus 是进程内队列，
跨进程/多机部署、崩溃后事件不丢的需求出现时再上；Outbox.relay 的投递目标
从内存队列换成 Redis Streams 即可，治理层不用改。见 PLAN.md。
