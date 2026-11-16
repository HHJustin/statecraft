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
  闲聊直通 Done 不进流水线。两级实现：`RuleTriage` 二分类（框架默认）；
  `IntentRouter` 多分类路由（`doc`/`review`/`explain`/`chitchat`）——
  **规则关键词打分定路由，灰区才交 LLM 兜底（可插槽，异常即降级）**，
  返回 `level=rule|llm|fallback` 与 `confidence` 便于统计规则覆盖率
- **三路混合检索记忆**：`HybridMemory` = dense 向量 + 词面 + **代码符号图**
  （`EmbeddingBase` / `SymbolIndex` 双协议，离线 `HashEmbedding` 零依赖）；
  `explain` 同时给出三路分值 + `query_symbols`；
  黄金集实测改述样本 Recall 0.600 → 1.000（第二路），
  关系型查询第三路从 0 召回 → 子函数全召回（`tests/test_codegraph.py`）
- **代码符号图**：`CodeGraph` 用标准库 `ast` 抽函数/类/参数/docstring/异常，
  回填 caller/callee 边（零依赖、确定性，不靠 NLP 猜实体）；
  符号文本只写「我是谁」，关系只在边上——职责分离，关系型查询只能由图回答
- **元数据前置过滤**：`search(where={...})` 先按 metadata 过滤再打分，
  支持单值 / 列表（命中任一）；RAG 只让规范类条目参与打分，噪声不进计算
- **Provider 工厂**：换 LLM / 记忆后端都只改配置字符串（`LLMFactory` / `MemoryFactory`，
  支持注册第三方）
- **委派 + 防死锁**：任务可委派子任务（`delegate_subtask` 工具，planner 专属）；
  `DelegationTracker` 双闸门——深度超限 / 委派给祖先成环，直接拒绝（P10）
- **Saga 补偿**：Tool 声明 `undo` 即自动记账 `SagaLog`；打回 / 取消时逆序补偿，
  尽力而为 + 审计留痕（Blocked 保留现场不补偿）（P6）
- **静态看板**：Task + 会话日志 → 单文件 HTML（任务卡片 / 流转时间线 / 告警与补偿），
  零依赖浏览器直开（P7）
- **官方示例一：codedoc** —— 代码注释生成流水线：
  意图路由四分类 → 三路检索（规范 RAG + 符号图）→ planner 规划 /
  writer 写入（Saga 可回滚）/ critic 审核，坏注释打回时自动撤销
- **官方示例二：code-review** —— 代码审查流水线，与 codedoc **共用同一套内核
  （`core/` 零改动）**，只换角色 / 契约 / 工具集：
  `record_finding` 工具强制结构化（文件+行号+严重度+证据）→ 六条确定性形式审
  （证据逐字符核对、未读即报、行号越界、覆盖度…）→ critic 抽查复核（meta-review）。
  真机实测：埋点检出 5/5、类别 5/5、干净文件误报 0、证据编造 0
- **真实仓库代码实测**（requests / click / flask，非自造靶子）：报出了官方库里的
  真实历史缺陷（`appnd_const` 文档笔误、`Vary: Cookie` 漏加），证据可逐字符核对；
  同时暴露 2 个致命缺陷（大文件无终止标记 → Blocked、388 行报 0 条双盲通过），
  已修 + 7 个回归用例。详见 `docs/真实代码实测报告.md`
- **审查结果持久化（P12）**：`statecraft/storage/` 把批次 / findings / 清白声明落 SQLite
  （标准库自带、单文件、能 SQL 聚合）。主键 `sha1(review_id|file|line)` —— 同一批次重跑
  不会翻倍；`findings_fts` 是派生索引，增量与重建共用**同一段 SQL 表达式**，所以「重建后
  结果一致」是可验证的而不是口头承诺。`--db` 指定库文件，`--no-db` 关掉落盘
- **记忆可落盘**：`SqliteMemory` 实现同一套 5 方法（md5 去重 / 阈值前置 / 软删 / history
  只增不改），`memory_backend="sqlite"` 切换即用，`MemoryFactory` 一行没改；
  元数据过滤下推到 `json_extract`，不是「先捞全表再 Python 筛」
- **Provider 错误分类**：429/5xx 可重试、401/404 立刻停；同样是 429，「限流」与
  「额度已用完」含义相反，按正文关键词区分。编排器把不可恢复错误降级成 Blocked，
  而不是让异常打穿主循环——否则限流一次就留下一个永远「正在跑」的批次
- **审查结果可问答（P13）**：`statecraft/qa/` 三层 —— 路由（规则判定，不用模型）
  → 查询（过滤走 SQL 聚合，语义走词面 + 可选三路召回）→ 答案（强制引用编号）。
  一条问题能不能查、怎么查、答案出处在哪，三层各自可单测。三条硬规则：
  **聚合数字不经过模型**（SQL 算的，模型只负责复述会算错）；**引用回查 DB**
  （模型编的 `[F7]` 会被就地标注「查无此记录」）；**空结果不调模型**
  （没有记录直接答「没有相关结论」，成本与幻觉都是零）
- **增量审查（P14）**：`--diff` 只审改动过的函数。`git diff --unified=0` 解析行区间 →
  `ast` 映射到最内层函数 → 函数体哈希一致则跳过，并**把上次的问题按新行号带回来**
  （跳过 ≠ 结论消失，否则报告会「上次报的问题这次全没了」）。三个真机才暴露的坑：
  未跟踪的新文件在 `git diff` 里根本不存在（静默报「没有可审的变更」）、单行函数与
  `<module>` 区间完全重合导致同一段代码审两遍、一个任务塞 186 个函数必然 Blocked
  → 按函数数分批（每批 ≤12 个），批内独立走完规划→审查→复核，结论累加到同一批次
- **命令行入口（P15）**：`python -m statecraft` 五个子命令 —— `review`（增量/指定文件/整仓）、
  `report`、`ask`、`reviews`、`units`（零成本预演「会审哪些函数」）。退出码分开：
  0 = 正常（含「没问题」）、1 = 业务失败（Blocked）、2 = 用法或环境错误（没 key、
  不是 git 仓库）。CI 门禁要拦「审出高危」，但不该把「没装 git」判成「审查不通过」
- **核心零依赖，测试全离线**（MockLLM 脚本化，281 个测试 20 秒跑完）
- **真实模型已验证**（DeepSeek V3 2026-11-11 / DeepSeek flash 2026-11-13）：
  连通 / 工具循环 / 端到端三链路通过；真模型下 critic 主动打回、Saga 逆序回滚
  3 个文件编辑、审产出靠 `read_file` 抓到「docstring 与实现不符」的真实缺陷。
  第一轮验证暴露并修复 7 个缺陷（MockLLM 测不出，见
  `tests/test_live_regressions.py` 12 个回归用例）；审查流水线又暴露 2 个
  （去重键过松导致同缺陷复述 22 条、对照文件自身不干净），见
  `examples/code-review/README.md`

## 快速开始

```bash
# 跑测试（离线）
python -m unittest discover -s tests -v

# 最小闭环演示 / 事件驱动演示 / 委派+补偿+看板演示 / 两条业务流水线
python demo.py
python demo_events.py
python demo_dashboard.py   # 生成 dashboard_demo.html
python examples/codedoc/codedoc.py                 # 注释生成（离线剧本）
python examples/codedoc/codedoc.py --live             # 注释生成（真实模型）
python examples/code-review/review.py --mock          # 代码审查（离线看流程）
python examples/code-review/review.py                 # 代码审查（需 OPENAI_API_KEY）

# 审任意目录的真实代码（--root 指定根，--file 逗号分隔）
python examples/code-review/review.py --root ../some-repo --file a.py,b.py

# 结果落 SQLite（默认仓库根 reviews.db；--no-db 关闭）
python examples/code-review/review.py --mock --db reviews.db
# CI 场景固定批次号 = 重跑覆盖同一批，同一处问题不会因为跑两遍变成两条
python examples/code-review/review.py --db reviews.db --review-id ci-<commit-sha>

# 直接查库（不需要额外工具）。注意 form_ok=1：被形式审驳回的编造条目也留档，
# 但不算「报告里的问题」——不加这个条件，分布会把它们算进去（6 vs 5）
sqlite3 reviews.db "SELECT severity, COUNT(*) FROM findings WHERE form_ok=1 GROUP BY 1"
sqlite3 reviews.db "SELECT file, COUNT(*) FROM findings WHERE severity='high' AND form_ok=1 GROUP BY 1"
sqlite3 reviews.db "SELECT COUNT(*) FROM findings WHERE form_ok=0"   # 审查者幻觉率分母

# 问答：查审查结果（P13）。不配 key 也能用，自动降级为确定性清单
python examples/code-review/ask.py --db reviews.db --q "target.py 有哪些高危问题？"
python examples/code-review/ask.py --db reviews.db            # 交互模式
python examples/code-review/ask.py --db reviews.db --no-llm   # 纯 SQL，不调模型
python examples/code-review/ask.py --db reviews.db --memory   # 启用三路召回
python examples/code-review/ask.py --db reviews.db --json --q "哪个文件问题最多？"

# 黄金集一键跑分（L1/L2/L3 离线评估）
python -m statecraft.eval.runner
```

### 命令行（P15，推荐入口）

`python -m statecraft` 与示例脚本共用同一份实现（示例只是薄壳）。

```bash
python -m statecraft review --path . --diff HEAD --mock   # 只审这次的改动（离线）
python -m statecraft review --path . --diff HEAD~3 --head HEAD --three-dot  # CI 比分支
python -m statecraft review --path src/ --severity high  # 整仓（配 --limit 控规模）
python -m statecraft units --path . --diff HEAD          # 预演：会审哪些函数（不花钱）
python -m statecraft review --path . --dry-run           # 同上，输出也含缓存命中情况

python -m statecraft report --db reviews.db              # 最近一次报告
python -m statecraft report --db reviews.db --review <id> --severity high
python -m statecraft ask "auth.py 有哪些高危问题" --db reviews.db --no-llm
python -m statecraft reviews --db reviews.db             # 列历史批次
```

`--json` 给脚本用；退出码 0/1/2 见上。**注意**：`--diff A --head B` 是在比两个提交，
而函数是按工作区文件解析的 —— 工作区脏了行号会错位，CLI 会打一行警告，
CI 上请先 checkout 到 B 再跑。

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

接上真模型后跑一次冒烟（连通性 → 工具循环 → 端到端三链路）：

```bash
# 环境变量方式（key 不进 shell 历史）
export OPENAI_API_KEY=sk-...            # Windows: set OPENAI_API_KEY=sk-...
export OPENAI_BASE_URL=https://api.deepseek.com/v1
export OPENAI_MODEL=deepseek-chat
python examples/live_smoke.py
```

失败会给出人话诊断（401 key 无效 / 404 模型名错 / 429 限流 / 网络不可达），
不甩裸 traceback。

**实测记录（DeepSeek V3，2026-11-11）**：

```
[1/3] 连通性      1.65s  → '通了'
[2/3] 工具循环    2.62s  真实调用 add_note 2 次
[3/3] 端到端      9.76s  Done
      流转 Planning → Reviewing → Planning → Reviewing → Executing → PendingConfirm → Done
```

真模型与 MockLLM 的行为差异（也是那轮修复的由来）：

| 现象 | MockLLM | 真实模型 | 修复 |
|---|---|---|---|
| 越权工具暴露 | 不调，测不出 | critic 真的调了 `read_file` | 暴露层与执行层共用 `resolve_policy` |
| 打回次数 | 按脚本 1 次 | 连续打回直到烧钱 | `max_rejections=2`，超限 Blocked |
| 审核者上下文 | 脚本不依赖 | 反复说「未提供待审内容」 | 注入上一步产出 |
| 「审核通过」去向 | 靠 DONE 标记区分 | 只说「审核通过」，回 Executing 重做 | 改由「进入 Reviewing 前的状态」决定 |
| 契约标记大小写 | 脚本全大写 | 可能输出 `reject` / `done` | 大小写不敏感匹配 |
| 审查者能不能核实产出 | 自述即结论 | 必须 `read_file` 才审得动 | 示例里给 review 角色只读权限 |

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
├── memory/        # 记忆层：5 方法协议 + 内存实现 + 三路混合检索
│                  #         dense（Embedding 协议）+ 词面 + 代码符号图（ast）
│                  #         元数据前置过滤 + 记忆后端工厂
├── storage/       # 持久层（P12）：SQLite schema / 连接与迁移 / ReviewStore
│                  #         DB 是真相源，FTS5 是派生索引（可一键重建）
├── qa/            # 问答层（P13）：规则路由 / 查询执行 / 强制引用的答案生成
├── incremental/   # 增量审查（P14）：diff 解析 / AST 函数单位 / 函数级缓存 / 审查计划
├── review/        # 审查流水线（P11 → P14）：领域模型 / 工具契约 / 装配 / 分批
├── cli.py         # 命令行入口（P15）：五个子命令 + 退出码
├── __main__.py    # python -m statecraft
├── providers/     # Provider 底座：工厂 + mock + openai 兼容 + 错误分类
├── eval/          # 黄金集评估：L1/L2/L3 跑分器 + 种子样本（JSONL）
├── dashboard.py   # 零依赖静态 HTML 看板
├── agents/        # 三层 prompt：GLOBAL.md → 角色文件
└── tests/         # 离线测试
```

## 路线图后续（未实现）

P14 增量审查 / P15 CLI 已完成（`--diff` + `python -m statecraft`）。仍未做：
**pip 打包**（`pyproject.toml` 已加、console script 指向 `statecraft.cli:main`，
但本机离线装不上 setuptools，`pip install -e .` 没实测过 —— 别当成已验证）；
L1-Code 标注集（漏报率目前无法度量）也还缺。P13 问答已完成，但它**不是 RAG**：
没有文档解析、没有 chunker、没有真 embedding 库、没有向量库，召回用的是
FTS5 + LIKE + 可选的 HybridMemory（HashEmbedding 伪向量）。别把它讲成 RAG 项目。

分布式事件总线（Redis Streams / NATS）——当前 EventBus 是进程内队列，
跨进程/多机部署、崩溃后事件不丢的需求出现时再上；Outbox.relay 的投递目标
从内存队列换成 Redis Streams 即可，治理层不用改。见 PLAN.md。
