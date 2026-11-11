# codedoc —— 代码注释生成与审查多 Agent 流水线

Statecraft 的官方应用示例：用治理框架构建一个「代码注释生成 Agent」。
它演示了框架的核心卖点在一个真实开发场景里如何协同工作。

## 多 Agent 分工

| 角色 | 职责 | 权限（AGENT_POLICY） |
|---|---|---|
| planner（doc_planner） | 读代码，产出"哪些函数要补 docstring"的计划 | read_file |
| executor（commenter） | 调用 `edit_file` 批量写入注释 | edit_file（带 undo 快照） |
| critic（review） | 审计划、审产出；打回即触发回滚 | 只读（无写权限） |

## 完整闭环（离线可复现）

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
python -m unittest tests.test_codedoc -v
```

## 对应的框架能力

| 场景需求 | Statecraft 机制 |
|---|---|
| 注释写入是文件副作用，必须可撤销 | Tool.undo + SagaLog 逆序补偿 |
| 坏注释不能直接进主干，先审再收尾 | 执行后审核（Executing→Reviewing 边 + REQUEST_REVIEW 标记） |
| writer 不能改逻辑、只能写注释 | 命令级权限白名单（commenter 角色只有 edit_file） |
| 写错了从哪来回哪去 | 状态机打回路径（Reviewing→Planning） |
| 全过程可审计 | JSONL 会话日志 + flow_log 留痕 |
