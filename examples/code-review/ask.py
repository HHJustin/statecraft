"""代码审查记录问答 CLI（P13 演示）。

用法：

    # 先跑一次审查落库（见 review.py）
    python examples/code-review/review.py --mock --db reviews.db

    # 问一句
    python examples/code-review/ask.py --db reviews.db --q "sample.py 有哪些高危问题？"

    # 交互模式
    python examples/code-review/ask.py --db reviews.db

    # 不接模型（纯 SQL/清单回答，零成本零幻觉）
    python examples/code-review/ask.py --db reviews.db --no-llm

默认行为：有 `OPENAI_API_KEY` 就用真实模型（列表类问题会归纳成一句话），
没有就自动降级为确定性清单 —— **不配 key 也能用**，这是有意的：
问答的核心价值是「查到」，不是「说得漂亮」。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent
if str(_ROOT) not in sys.path:                 # 允许直接 python ask.py
    sys.path.insert(0, str(_ROOT))

from statecraft.qa.answer import CodebaseQA        # noqa: E402
from statecraft.storage.findings import ReviewStore  # noqa: E402

DEFAULT_DB = str(_ROOT / "reviews.db")


def make_llm():
    """有 key 用真模型；--mock 用脚本化假模型；--no-llm 返回 None。"""
    if "--no-llm" in sys.argv:
        return None
    if "--mock" in sys.argv:
        from statecraft.providers.base import LLMReply
        from statecraft.providers.mock import MockLLM

        # 演示用：按上下文里第一条记录拼一句带引用的回答
        def handler(messages, tools):
            ctx = messages[-1]["content"]
            first = next((ln for ln in ctx.splitlines() if ln.startswith("[F1]")), "")
            return LLMReply(content=f"根据审查记录，最需要先处理的是：{first[5:].strip()} [F1]")

        return MockLLM(handler=handler)
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        return None
    from statecraft.providers.openai_compat import OpenAICompatLLM

    base = os.environ.get("OPENAI_BASE_URL", "https://www.apigoto.com/v1")
    model = os.environ.get("OPENAI_MODEL", "deepseek-flash")
    print(f"[ask] 真实模型: {model} @ {base}")
    return OpenAICompatLLM(base_url=base, model=model, api_key=key)


def build_qa(db: str, memory: bool = False) -> CodebaseQA:
    store = ReviewStore(db)
    mem = None
    if memory:
        from statecraft.memory.sqlite import SqliteMemory

        mem = SqliteMemory(db)             # 与 findings 共用同一个库文件
        qa = CodebaseQA(store, llm=make_llm(), memory=mem)
        n = qa.index_findings()            # 语义召回的候选源，DB 仍是真相源
        # 说「确保」而不是「新增」：memory.add 有 md5 去重，重复跑不会翻倍，
        # 但 add 的返回值不区分新旧，报「新增 N 条」就是在猜。
        print(f"[ask] 已确保 {n} 条 finding 在记忆层中（供三路召回，重复的不新增）")
        return qa
    return CodebaseQA(store, llm=make_llm())


def show(ans, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(ans.to_dict(), ensure_ascii=False, indent=2))
        return
    print(ans)
    if "--notes" in sys.argv and ans.notes:
        print("\n判定依据：")
        for n in ans.notes:
            print(f"  · {n}")


def main() -> None:
    db = DEFAULT_DB
    question = None
    review_id = None
    for i, a in enumerate(sys.argv):
        if a == "--db" and i + 1 < len(sys.argv):
            db = sys.argv[i + 1]
        elif a in ("--q", "--question") and i + 1 < len(sys.argv):
            question = sys.argv[i + 1]
        elif a == "--review" and i + 1 < len(sys.argv):
            review_id = sys.argv[i + 1]

    if not Path(db).exists():
        raise SystemExit(f"库不存在：{db}\n先跑一次审查落库：\n"
                         f"  python examples/code-review/review.py --mock --db {db}")

    qa = build_qa(db, memory="--memory" in sys.argv)
    as_json = "--json" in sys.argv

    if question:
        show(qa.ask(question, review_id=review_id), as_json)
        return

    print("代码审查记录问答（空行退出）。先跑一次 review.py 落库，再回来问。")
    print("例：「哪个文件问题最多？」「sample.py 有哪些高危问题？」\n")
    while True:
        try:
            line = input("问> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            break
        show(qa.ask(line, review_id=review_id), as_json)


if __name__ == "__main__":
    main()
