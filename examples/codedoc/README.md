# codedoc —— 代码注释生成与审查多 Agent 流水线

Statecraft 的官方应用示例：用治理框架构建一个「代码注释生成 / 审查 Agent」。
它演示了框架的核心卖点在一个真实开发场景里如何协同工作。

## 三块能力叠在一起

```
用户一句话
   │
   ├─ ① 意图路由（四分类）   补注释 doc / 审查 review / 解释 explain / 闲聊 chitchat
   │      规则打分定路由，灰区交 LLM 兜底（可插槽，异常即降级）
   │
   ├─ ② 三路检索（dense + 词面 + 代码符号图）
   │      符号图由 ast 抽取：签名 / 异常 / caller / callee
   │      关系型查询（"create_order 依赖哪些子函数"）只有图答得上来
   │
   └─ ③ 多角色治理流水线     planner → critic → executor（写坏可回滚）
```

## 多 Agent 分工

| 角色 | 职责 | 权限（AGENT_POLICY） |
|---|---|---|
| planner（doc_planner） | 读代码 + 符号图摘要，产出"哪些函数要补 docstring"的计划 | read_file |
| executor（commenter） | 调用 `edit_file` 批量写入注释 | edit_file（带 undo 快照） |
| critic（review） | 审计划、审产出；打回即触发回滚 | 只读（无写权限） |

## ① 意图路由：为什么这里需要四分类

同一个入口进来四种活，路由错了整条流水线白跑：

| 输入 | route | level | 说明 |
|---|---|---|---|
| 给 sample.py 的三个函数补充 docstring | `doc` | rule | 关键词 `docstring` |
| 审查一下这段代码的注释质量 | `review` | rule | 动词权重 > 名词：`审查`(3)+`质量`(1) 胜 `注释`(1) |
| 解释一下 apply_coupon 是干什么的 | `explain` | rule | `解释`(3)+`是干什么`(3)+`干什么`(2) |
| 这段东西帮我看看 | `explain` | **llm** | 规则无命中 → LLM 兜底 |
| 在吗？ | `chitchat` | rule | 寒暄整句匹配 → 分拣直通 Done |

三级策略（不做的第四级也写清了理由）：

- **L1 规则**：零成本、可离线、可代码断言，覆盖绝大多数明确输入
- **L2 LLM 兜底**：只在规则没命中/多路僵持时调用，成本压到最低，**异常即降级**
- **兜底**：默认路由（保守），`confidence` 低便于上层人工介入
- **不做小模型级**：小模型的价值是用低延迟挡住 80% 流量省钱，只在高 QPS、成本敏感的场景划算；codedoc 是"点一下/PR 触发一次"的稀疏流量，加第三级是过度设计

## ② 三路检索：代码符号图不是"通用实体图"

Mem0 的第三路是业务实体图（靠 NLP 抽取）；代码场景有更好的来源 —— **AST**：

```
ast.parse(source) → 函数/类/参数/docstring/raise/调用
                  → SymbolNode（我是什么）
                  → caller / callee 边（我和谁有关系）
```

**职责分离**（这是关键设计）：

- 文本管「我是谁」：`create_order(user, items, coupon)｜order_flow.py:17｜无 docstring`
- 图管「我和谁有关系」：`create_order → apply_coupon / validate_stock / send_receipt`

把关系写进文本，词面/向量检索会"假装"具备关系能力，反而掩盖第三路的价值。
分开之后，关系型查询只有图答得上来：

```
query: 'create_order 依赖哪些子函数'
双路 top5: create_order[0.13/0.28/0.00] parse_config[0.00/0.32/0.00]
          calc_discount[0.00/0.25/0.00] send_email[0.00/0.23/0.00]
          validate_stock[0.00/0.22/0.00]        ← 只有 dense 噪声，两个真子函数掉出前 5
三路 top5: create_order[0.13/0.28/1.00] validate_stock[0.00/0.22/0.50]
          apply_coupon[0.00/0.21/0.50] send_receipt[0.00/0.13/0.50]
          parse_config[0.00/0.32/0.00]          ← 图把真子函数确定性顶到 2–4 位
```

分组说明：`[词面分/向量分/图分]`，图分 = 直接命中 1.0、一跳邻居 0.5、无关 0。

## ③ 完整闭环（离线可复现）

```
Created → Planning → Reviewing → Executing →(REQUEST_REVIEW)→ Reviewing
  →(critic 打回：注释未覆盖异常行为)
  → 【Saga 逆序回滚：已写入的坏注释被自动撤销】
  → Planning → Reviewing → Executing → Reviewing
  →(critic 通过)→ PendingConfirm → Done
```

坏注释在打回时被自动回滚、好注释在重写后生效——**LLM 写错不可怕，
可怕的是写错的东西留在代码库里没人能撤**。这正是治理层的价值。

## 运行

```bash
# 在仓库根目录
PYTHONPATH=. python examples/codedoc/codedoc.py
# 或
python -m unittest tests.test_codedoc tests.test_codegraph tests.test_intent_router -v
```

## 对应的框架能力

| 场景需求 | Statecraft 机制 |
|---|---|
| 同一入口四种活，路由错了白跑 | `IntentRouter` 四分类（规则打分 + LLM 兜底插槽 + 保守默认） |
| 注释质量取决于项目规范/术语，不能裸写 | **RAG**：术语表 + 规范入 `HybridMemory`，写前检索注入（元数据前置过滤只取规范类） |
| 注释该写清前置条件/副作用，需要调用关系 | **代码符号图**：ast 抽 caller/callee，`SymbolIndex` 提供第三路打分 |
| 注释写入是文件副作用，必须可撤销 | `Tool.undo` + `SagaLog` 逆序补偿 |
| 坏注释不能直接进主干，先审再收尾 | 执行后审核（Executing→Reviewing 边 + `REQUEST_REVIEW` 标记） |
| writer 不能改逻辑、只能写注释 | 命令级权限白名单（commenter 角色只有 `edit_file`） |
| 写错了从哪来回哪去 | 状态机打回路径（Reviewing→Planning） |
| 全过程可审计 | JSONL 会话日志 + flow_log 留痕 |
