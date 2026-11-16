# 代码审查流水线（code-review）

Statecraft 作为「框架」的第二个落地应用。与 `codedoc`（注释生成）**共用同一套内核**，
差别只有三处：Agent 的 system prompt、工具集、权限白名单。

流程完全一样 —— 这是「框架 vs 应用」最硬的证据：

```
Created → Planning → Reviewing → Executing → Reviewing → PendingConfirm → Done
          规划审查    审计划      执行审查    审报告      二次确认      出报告
```

零内核改动：`statecraft/core/` 一行没改。

---

## 跑起来

```bash
# 离线看流程（无需 key，脚本化假模型跑完整状态机）
python examples/code-review/review.py --mock

# 真实模型
set OPENAI_API_KEY=sk-...
set OPENAI_BASE_URL=https://www.apigoto.com/v1
set OPENAI_MODEL=deepseek-flash
python examples/code-review/review.py

# 结果落 SQLite（P12；默认仓库根 reviews.db，--no-db 关闭）
python examples/code-review/review.py --mock --db reviews.db
# 固定批次号 = 重跑覆盖同一批（同一处问题不会因为跑两遍变成两条）
python examples/code-review/review.py --db reviews.db --review-id ci-<sha>

# 审查结果问答（P13，先落库再问）
python examples/code-review/ask.py --db reviews.db --q "target.py 有哪些高危问题？"
python examples/code-review/ask.py --db reviews.db            # 交互模式
python examples/code-review/ask.py --db reviews.db --no-llm   # 纯 SQL，零成本

# 换文件（默认审 target.py；审干净文件看误报率）
python examples/code-review/review.py --file target_clean.py
```

---

## 真实模型实测（DeepSeek flash）

| 指标 | 结果 |
|---|---|
| 埋点检出率 | **5/5 = 100%**（类别也 5/5 对得上） |
| 干净文件误报 | **0 条** |
| 证据编造 | **0 条**（六条形式审全过） |
| 额外发现 | 1 条真实问题（`DEFAULT_CHANNEL` 定义未用 + 渠道比较大小写敏感） |
| 端到端 | `Created → … → Done`，耗时约 40 秒 |

同一套靶子文件在 MockLLM 下也跑出 5/5 —— 但注意：**Mock 那 5 条是我写死的标准答案**，
它证明流程通，不证明审查能力。审查能力只有真模型那次跑分算数。

### 真实仓库代码（requests / click / flask，2026-11-13）

靶子不是自造的 `target.py`，是 GitHub 上的真实代码（下载脚本见 `E:\Study\探针\fetch_code.py`）：

| 文件 | 规模 | 结果 |
|---|---|---|
| `requests/hooks.py` @v2.31.0 | 33 行 | 4 条 low（都成立：变量语义漂移 / 模块级可变 list / 缺返回契约），1 条被 critic 推翻 |
| `click/parser.py` @7.1.2 | 428 行 | 4 条，含 `appnd_const` 文档笔误、`parse_args` 就地消费调用方列表 |
| `flask/sessions.py` @1.1.4 + `hooks.py` | 421 行 | 2 条真发现 + 1 条清白声明；`Vary: Cookie` 漏加是 Flask 2.x 真实修过的缺陷 |

这轮实测暴露并修掉两个致命缺陷（详见 `docs/真实代码实测报告.md`）：

1. **428 行文件 → Blocked**：`max_tool_iters=4` 装不下 15 条 finding，轮次耗尽连总结都
   没机会输出；编排器按"有没有 REQUEST_REVIEW"判失败 → 重跑 → Blocked，产出全废。
   修法：轮次耗尽补一次「不许调工具只输出结论」的收尾轮 + 用 `agent.mutations`
   （有副作用工具成功执行次数）判断"确实干了活"。
2. **388 行报 0 条却 Done**：reviewer 保守到一条不报，critic 也没有"该报的报了吗"的视
   角，形成双盲通过。修法：内核加 `completion_check` 扩展点，应用注册"每个文件必须
   有结论"；配套 `declare_clean`（清白声明）与 `review_status`（critic 查覆盖缺口）。

---

## 三个设计决定

### 1. 结构化靠工具强制，不靠模型自觉输出 JSON

错误做法是让模型吐 JSON 再正则解析：它会在前面加「以下是我的审查结果」、会漏字段、
会把 `line` 写成字符串、长输出还会截断。

这里提供 `record_finding` 工具，参数 schema 卡死必填项；缺 `line` 直接失败并回填
错误，模型被迫补齐。顺带天然进 `AUDIT_LOG`，事后能查是谁在什么时候记的。

### 2. `evidence` 逐字符核对 → 把内容幻觉变成确定性检测

要求模型从源文件**原样复制 1-3 行**作为证据。比对不上 = 编造，不需要第二个 LLM 当裁判。

这就是「状态机防不了内容幻觉」的补丁：状态机管流程（结构正确），
证据核对管内容（事实正确）。

### 3. 六条确定性形式审

| # | 检查 | 拦什么 |
|---|---|---|
| 1 | 必填字段完整 | 模型偷懒只写「这里有问题」 |
| 2 | `severity`/`category` 在枚举内 | 自造 `critical` 之类不存在的档位 |
| 3 | 文件在本次审查范围内 | 报了个没让审的文件（越界） |
| 4 | 行号未越界 | 幻觉行号（`line: 9999`） |
| 5 | 证据可在源文件中核对 | **编造代码** |
| 6 | 本会话确实读过该文件 | **未读即报**（`reads` 记录 + `AUDIT_LOG` 双证） |

第 6 条是白名单机制的隐藏价值：**命令级白名单 + 审计日志不只是安全设施，
还是审查者的不在场证明系统** —— 它声称在 `target.py:42` 发现问题，
但本会话没有 `read_file("target.py")` 记录，那这份报告只能是想象的。

---

## 「谁来审审查者」

审查报告本身也是产出，也会漏报、误报、编造。递归不能靠「养一个更高级的审查者」，
而是在**证据变便宜处截断**：

```
代码       → 审它最贵（要读懂语义）→ LLM，概率性
审查报告   → 审它便宜（短、结构化）→ 规则 + 抽查
流程契约   → 审它零成本（标记词）  → 状态机，确定
```

所以 `Reviewing` 的第二轮里，critic 只做**抽查**（`confirm_finding` 1-2 条），
不重审全量 —— 全量重审等于审两遍，成本翻倍还会出现两个审核者意见打架。

---

## 两个真机踩出来的坑（MockLLM 测不出）

### 坑 1：同一处缺陷换措辞复述 → 22 条里 17 条是重复

第一版按「文件+行+类别+message」哈希去重，模型把 `average()` 空列表问题
换三种说法报了三遍，报告从 5 条变成 22 条。

收紧到「文件+行+类别」后，它又换个类别再报一遍（句柄泄漏同时记成
`correctness` 和 `exception`）。

最终按「**文件+行**」去重，理由不是偷懒，而是审查评论的天然粒度：
评论挂在行上，同一行挂两条重复评论只会让提交者困惑。

### 坑 2：「干净文件」其实不干净

模型在 `target_clean.py` 上报了 `MAX_RETRY` 未使用 —— 查了一下，**它是对的**，
我的对照文件里这个常量确实没有任何引用。另外 `format_money` 用 `f"{cents/100:.2f}"`
也有二进制浮点误差。

修完靶子文件后重跑，误报降到 0。

**这次教训比代码本身值钱**：审查系统报出问题时，第一反应不该是「它误报了」，
而该是「先核实一下」——否则你调的不是审查器，是自己的心理防线。

---

## 落库（P12）

`ReviewSession.attach(store, review_id)` 之后，每条 finding / 清白声明 / 复核结论都会
写进 SQLite。三个设计点：

| 决定 | 理由 |
|---|---|
| 主键 `sha1(review_id\|file\|line)[:16]` | 模型并行发多次 `tool_call` 是常态，内容哈希天然去重；message 不进哈希（换措辞就失效） |
| 冲突时 UPDATE 而非 `INSERT OR IGNORE` | 形式审驳回后带着正确证据重新提交是正常流程，IGNORE 会让它永远停在驳回态 |
| FTS 文本由 `SEARCH_TEXT_SQL` 生成，增量与重建共用 | 否则「重建结果一致」只是愿望 |

**DB 是真相源，索引是派生视图**：`findings_fts` 坏了不修，直接
`store.rebuild_index()` 重建（`DELETE` + 从 `findings` 重灌，与增量写共用同一段 SQL）。

落库时机与终态：

```
record_finding  → INSERT ... ON CONFLICT(id) DO UPDATE（幂等）
confirm_finding → UPDATE findings SET status='confirmed'|'disputed'
任务 Done       → reviews.status='done'   + 统计列按 findings 重算
任务 Blocked    → reviews.status='blocked'（保留全部 findings —— 它们最该留档）
运行异常        → 按 blocked 落库，绝不留一个「正在跑」的批次
```

最后一条是真机踩出来的：接口额度耗尽（HTTP 429 / code 42906）时，旧代码让异常打穿
主循环，任务既不 Done 也不 Blocked，库里就躺着一个永远 `draft` 的审查。现在 provider
错误会分类（429/5xx 可重试、401/404 立刻停、「额度已用完」虽为 429 但不可重试），
不可恢复时降级为 Blocked —— 实测 1.3 秒收敛，而不是白等 90 秒重试。

查库举例（不需要额外工具）：

```bash
sqlite3 reviews.db "SELECT severity, COUNT(*) FROM findings WHERE form_ok=1 GROUP BY 1"
sqlite3 reviews.db "SELECT file, COUNT(*) FROM findings WHERE severity='high' AND form_ok=1 GROUP BY 1"
sqlite3 reviews.db "SELECT COUNT(*) FROM findings WHERE form_ok=0"   # 编造/越界被驳回的条数
sqlite3 reviews.db "SELECT status, COUNT(*) FROM reviews GROUP BY 1"
```

`form_ok=1` 这个条件不能省：被形式审驳回的编造条目**也留在表里**（否则幻觉率没法算），
但在报告里不算数。`store.counts_by_severity()` 默认就带这个过滤——真机第一次跑出来
「报告 5 条、分布 6 条」就是这么来的，已修。

实测（`--mock`，3 次重跑同一 `--review-id`）：始终 1 个批次 / 5 条有效 finding，
严重度分布 `{high:3, medium:2}` 与 `findings_total=5` 对得上，
`DROP TABLE findings_fts` 后重建，检索结果逐条一致；干净文件走 `declare_clean`
时 `clean_claims` 落库（0 条 finding 也有结论可查）。

---

## 问答（P13）

审完的结论躺在表里，只有会写 SQL 的人能查 —— 这等于没交付。`ask.py` 是那个
「人能用」的入口，三层各管一件事：

| 层 | 干什么 | 不用模型的原因 |
|---|---|---|
| `qa/router.py` | 判定走 SQL 还是走召回 | 问答对延迟敏感；规则错判能改词表，模型错判没法复现 |
| `qa/query.py` | 抽槽位 → 执行 SQL / 召回 | 过滤条件必须精确，「高危」不是一个语义概念 |
| `qa/answer.py` | 组装上下文 → 生成 → 解析引用 | 引用校验是确定性的，不需要模型 |

### 两类问题，两条路径

```
「target.py 有哪些高危问题？」  → WHERE file=? AND severity='high'   （过滤）
「哪个文件问题最多？」          → GROUP BY file ORDER BY COUNT(*) DESC（聚合）
「有没有处理过类似的除零问题？」 → 词面召回 + 可选三路召回           （相似）
```

用向量答第一类会漏条目；用 SQL 答第三类根本不成立（「修过吗」没有可过滤的字段）。

**路由凭什么判**：结构化词（哪些/多少/最多/分布）与语义词（类似/以前/处理过）
各 2 分，高分胜出。平局时看有没有**结构化锚点**（文件名 / 严重度词 / 聚合词）——
「哪些问题跟这个类似」没有锚点，判语义；「sample.py 和这个类似的问题有哪些」
有锚点，判过滤。一个信号都没有就走结构化（结果可复现、可断言）。

两个词表冲突是实测踩出来的，写进了回归用例：
- **「列表」不能当结构化词** —— 它在 Python 语境是 list，「空列表相除会怎样」
  会被判成「列个清单给我」；
- **「X 处理过吗」必须算语义** —— 它一个结构化词都不含，漏了就会返回全库清单。

### 三条硬规则

**1. 聚合数字不经过模型。**「哪个文件问题最多」的数字由 SQL 算出，直接作为答案
（`source="sql"`）。让模型复述一遍只会引入说错数字的风险 —— 这是唯一一处
「有模型也不用」的地方。

**2. 引用必须回查 DB。** 上下文里每条记录带编号（`[F1] [F2]`），prompt 硬性要求
每个结论标引用。模型编的 `[F7]` 会被：① 记进 `unknown_citations`；② 在文本里
就地标注成 `[F7·查无此记录]`（不悄悄删掉 —— 删掉等于替它掩盖了一次幻觉）；
③ 正常引用还会再查一次库，确认那条记录还在。**答案里的每个编号都能落到一行
数据库记录**，这是可断言的事实。

**3. 空结果不调模型。** 库里没有相关记录，直接答「现有审查记录中没有相关结论」，
一次模型调用都不发（测试用 `call_count == 0` 守着）。空着让模型「根据上下文回答」，
它一定会倒出上下文之外的东西。

### 两个降级路径（都有意为之）

- **没有 key** → 列表类问题给确定性清单（`source="records"`），照样能用。
  问答的核心价值是「查到」，不是「说得漂亮」。
- **模型报错/额度用尽** → 同上降级，并在 notes 里写明原因。实测真实 429
  时 1 秒内给出清单，不是抛异常给用户看。

### 中文问句要先改写

`ReviewStore.search` 的 FTS5 只切 ASCII 词，中文走 LIKE 时是**整句匹配** ——
「有没有处理过类似的除零问题」去 LIKE `%整句%`，库里永远没有这一行，召回率
是 0。所以 `keywords()` 先剥掉疑问词/虚词，再切出 `除零`：

```
「有没有处理过类似的除零问题」 → ["除零"] → LIKE '%除零%' → 命中
「average 空列表」            → ["average", "空列表"]（英文标识符优先）
```

### 记忆层只是召回器，不是答案来源

`--memory` 会把 findings 灌进 `SqliteMemory`（metadata 带 `finding_id`），
让三路召回（词面 / 向量 / 代码图）参与候选。但**每条召回都要回 DB 复核**：
库里查不到的（索引过期、记录被删）直接丢弃，并在 notes 里报「N 条查无实据」。
索引可以脏，答案不能脏。

### 实测

```
$ ask.py --db p13.db --no-llm --q "target.py 有哪些高危问题？"
[structured][未经模型]
共 3 条（高危 3 条）：
[F1] target.py:23 [high/boundary] average() 对空列表直接除零 …
[F2] target.py:42 [high/exception] except Exception + pass 把投递失败静默吞掉 …
[F3] target.py:56 [high/correctness] 可变默认参数 bucket=[] 跨调用共享 …

$ ask.py --db p13.db --no-llm --q "哪个文件问题最多？"
[structured][精确统计]
问题最多的文件：
  1. target.py —— 5 条

$ ask.py --db p13.db --no-llm --q "有没有处理过类似的除零问题？"
[semantic][未经模型]
共 1 条（高危 1 条）：
[F1] target.py:23 …          # 查询改写：除零

$ ask.py --db p13.db --no-llm --q "有没有处理过内存泄漏问题？"
[semantic][无记录]
现有审查记录中没有相关结论。      # 一次模型调用都没发
```

**多批次口径**（这是最容易答错的地方）：同一个问题在 v1/v2 两个批次都存在时，
「一共有多少问题」答 **1**（只看最新批次），「以前处理过类似的吗」答 **2**
（跨批次）。两个口径都印在 notes 里。

---

## 增量审查（P14）

整仓重跑没人愿意用。`--diff` 只审改动过的函数：

```bash
python -m statecraft review --path . --diff HEAD --mock      # 工作区 vs HEAD
python -m statecraft review --path . --diff HEAD~1 --head HEAD   # 两个提交之间
python -m statecraft review --path . --diff origin/main --three-dot  # CI 比分支（merge-base）
python -m statecraft units  --path . --diff HEAD             # 只预演范围，不调模型
```

链路：`git diff --unified=0` → 行区间 → `ast` 映射到**最内层**函数 → 函数体哈希
（`sha1(qualname + 归一化函数体)`，忽略空行与行尾空格）→ 命中缓存就跳过。

四个「看起来能用其实不能用」的坑，都踩过并修了：

| 坑 | 不修会怎样 |
|---|---|
| `git diff` 不显示未跟踪文件 | 新建 200 行还没 `git add`，报「没有可审的变更」，不报错不提示 |
| 一个任务审不完一整仓 | 186 个函数 → 轮次预算耗尽 → 整批 `Blocked`，连已有的正确结论一起废掉。现在按函数数分批（每批 ≤12） |
| 清白声明只有整文件级 | 文件里报了 2 条问题，剩下 18 个函数永远「无结论」，闸门不放行。现在 `declare_clean(units=[...])` 可按函数声明 |
| 跳过 = 结论消失 | 第二次报告会「上次报的问题这次全没了」。现在按新行号带回并标「复用」 |

实测（statecraft 自己仓库，`--diff HEAD`）：15 个文件 / 196 个函数 → 19 批全 `Done`；
第二次跑 196 个函数全部命中缓存、不调模型，仍带回 3 条结论。

**`--diff A --head B` 注意**：行区间来自 B，函数却按工作区文件解析。工作区脏了会
提示「工作区与 B 不一致，行号可能错位」—— CI 上先 checkout 到 B。

---

## 命令行（P15）

示例脚本是薄壳，真正的入口在包里：

```bash
python -m statecraft review --path . --diff HEAD --mock --db reviews.db
python -m statecraft report --db reviews.db [--review <id>] [--severity high]
python -m statecraft ask "target.py 有哪些高危问题？" --db reviews.db --no-llm
python -m statecraft reviews --db reviews.db
python -m statecraft units --path . --diff HEAD
```

退出码：`0` 正常（含「没有问题」）/ `1` 业务失败（Blocked、查无批次）/
`2` 用法或环境错误（没 key、不是 git 仓库、路径不存在）。
CI 门禁靠退出码，所以要拦「审出高危」，但不该把「没装 git」判成「审查不通过」。

---

## 局限（诚实清单）

- ~~**无 diff**~~：P14 已解决（`--diff`，见「增量审查」一节）；不传 `--diff` 仍是整文件审。
- **无跨文件上下文**：不知道被审函数在别处怎么被调用（符号图能力没接进来）。
- **抽查不是全量复核**：critic 只核对 1-2 条，剩下的靠形式审兜底。
- **行号整行匹配**：不精确到列，也不支持多行区间。
- ~~**结果只进内存**~~：P12 已解决，见下节「落库」；未落库时仍可 `--no-db` 跑。
- **跨行重复未根治**：去重键是「文件+行」，同一处缺陷写在连续两行（如 `hooks = hooks or {}` / `hooks = hooks.get(key)`）会报两条，目前靠 critic 推翻一条。
- **问答只覆盖「审过的东西」**：问「这个函数在哪被调用」答不了 —— 库里只有 findings，没有符号表（P13 没做 `symbols` 表）；问「为什么这么设计」也答不了，那不在审查记录里。
- **路由是规则词表**：中文问法变化多，词表覆盖不到的句式会静默走结构化（拿全库清单）。所以每次回答都带 notes 写明路由理由，方便发现漏判。
