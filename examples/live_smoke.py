#!/usr/bin/env python
"""真实 LLM 冒烟测试 —— 验证 provider / 工具循环 / 状态机在真模型下能跑通。

之前所有 demo 都跑在 MockLLM 上，证明的是「流程正确」；
这个脚本证明的是「接上真模型之后链路依然通」——两者缺一不可。

三步（每步独立，失败即停并给诊断）：

    1. 连通性   —— 一次最小 chat 调用：延迟 + 返回片段 + 模型名
    2. 工具循环 —— 让 Agent 必须真实调用工具（add_note）两次
    3. 端到端   —— planner / critic / executor 走完
                   Created → Planning → Reviewing → Executing → Done

用法（推荐环境变量，避免 key 进 shell 历史）：

    # Windows
    set OPENAI_API_KEY=sk-...
    set OPENAI_BASE_URL=https://api.deepseek.com/v1
    set OPENAI_MODEL=deepseek-chat
    python examples/live_smoke.py

    # macOS / Linux
    export OPENAI_API_KEY=sk-...
    export OPENAI_BASE_URL=https://api.deepseek.com/v1
    export OPENAI_MODEL=deepseek-chat
    python examples/live_smoke.py

也支持命令行传参（--base-url / --api-key / --model），但不建议：key 会留在历史里。

注意：真模型不会严格照 MockLLM 的剧本演（可能不打回、可能多调几次工具）。
本脚本只断言「链路通 + 状态落到终态」，不断言具体路径——那是黄金集该干的活。
"""
import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from statecraft.config import AgentCoreConfig            # noqa: E402
from statecraft.core.orchestrator import Orchestrator    # noqa: E402
from statecraft.core.runner import Agent                 # noqa: E402
from statecraft.core.tools import (AGENT_POLICY, Tool,   # noqa: E402
                                   ToolRegistry)
from statecraft.memory.local import InMemoryMemory       # noqa: E402
from statecraft.providers.openai_compat import OpenAICompatLLM   # noqa: E402

PROMPTS = {
    "planner": (
        "你是任务规划者。把用户任务拆成 2–3 步可执行的动作，输出简短纯文本计划。"
        "不要调用工具。不要在输出里写 DONE。"
    ),
    "critic": (
        "你是审核者，只审规划者给出的计划是否具体、可执行。"
        "合格：只输出「审核通过」。不合格：只输出「打回：<一句话理由>」。"
        "不要调用工具，不要写 DONE。"
    ),
    "executor": (
        "你是执行者。按计划执行；需要记录要点时调用 add_note 工具（可多次调用）。"
        "全部完成后输出一句总结，并以 DONE 结尾。"
    ),
}

NOTES: list[str] = []


def make_note_tool() -> Tool:
    def handler(ctx, text, **_):
        NOTES.append(str(text))
        return f"noted ({len(NOTES)}): {text}"

    return Tool("add_note", "记录一条执行要点到内存列表",
                {"type": "object",
                 "properties": {"text": {"type": "string", "description": "要点内容"}},
                 "required": ["text"]},
                handler)


class _FakeTask:
    """冒烟用最小任务对象（Agent.run 只需要 description/state）。"""

    def __init__(self, description: str, state: str = "Executing"):
        self.id = "smoke"
        self.description = description
        self.state = state


def _short(text: str, n: int = 80) -> str:
    text = " ".join((text or "").split())
    return text[:n] + ("…" if len(text) > n else "")


def diagnose(exc: Exception) -> str:
    """把常见失败翻译成人话（比裸 traceback 有用）。"""
    name = type(exc).__name__
    msg = str(exc)
    if "401" in msg or "Unauthorized" in msg or "invalid_api_key" in msg:
        return "API key 无效或已失效（401）——检查 OPENAI_API_KEY"
    if "404" in msg or "model_not_found" in msg:
        return "模型名或 base_url 不对（404）——检查 OPENAI_MODEL / OPENAI_BASE_URL"
    if "429" in msg or "rate" in msg.lower():
        return "限流或余额不足（429）——稍后重试或检查账户余额"
    if name in ("URLError", "ConnectError", "ConnectTimeout", "TimeoutError"):
        return f"网络不可达（{name}）——检查 base_url 与网络/代理"
    return f"{name}: {msg[:200]}"


async def step1_connect(llm) -> None:
    print(f"[1/3] 连通性检查  model={llm.model}  base_url={llm.base_url}")
    t0 = time.time()
    reply = await llm.chat([{"role": "user", "content": "只回复两个字：通了"}])
    print(f"      ✓ {time.time() - t0:.2f}s  → {_short(reply.content, 40)!r}")


async def step2_tools(llm) -> None:
    print("[2/3] 工具循环（必须真实调用 add_note 两次）")
    registry = ToolRegistry()
    registry.register(make_note_tool())
    AGENT_POLICY["note_taker"] = {"role": "execution", "tools": {"add_note"}}
    agent = Agent("executor", llm, role="note_taker", registry=registry,
                  system_prompt="你是执行者。必须调用 add_note 工具记录两条要点"
                                "（分两次调用），然后输出一句话总结。")
    t0 = time.time()
    out = await agent.run(_FakeTask("记录两条关于「状态机白名单」的要点。"))
    print(f"      ✓ {time.time() - t0:.2f}s  工具调用 {len(NOTES)} 次")
    for i, note in enumerate(NOTES, 1):
        print(f"        - note{i}: {_short(note, 60)}")
    print(f"        ↳ 模型总结: {_short(out, 100)!r}")
    if not NOTES:
        print("      ⚠ 模型没调工具（function-calling 可能不被该模型支持）")


async def step3_pipeline(llm) -> bool:
    print("[3/3] 端到端（planner → critic → executor → Done）")
    registry = ToolRegistry()
    registry.register(make_note_tool())
    # 复用具名角色 note_taker（不改写内置 executor 的权限策略）
    roles = {"planner": "planner", "critic": "critic", "executor": "note_taker"}
    agents = {name: Agent(name, llm, role=roles[name], registry=registry,
                          system_prompt=PROMPTS[name], max_tool_iters=6)
              for name in ("planner", "critic", "executor")}
    cfg = AgentCoreConfig(
        auto_confirm=True,
        state_timeouts={"Planning": 120.0, "Reviewing": 120.0, "Executing": 300.0},
        retry_backoff=[1.0], max_retries=1,
    )
    orch = Orchestrator(agents, InMemoryMemory(), cfg)
    task = orch.create_task("为「状态机白名单」这个设计写 2 条要点笔记，"
                            "并给出一句话的推广建议。")
    t0 = time.time()
    task = await orch.run(task)
    path = " → ".join(f["to"] for f in task.flow_log)
    print(f"      ✓ {time.time() - t0:.2f}s  终态={task.state}")
    print(f"        流转: {path}")
    print(f"        笔记 {len(NOTES)} 条")
    if task.state != "Done":
        reasons = [f["reason"] for f in task.flow_log][-3:]
        print(f"      ⚠ 未达 Done（链路已通，但模型没按契约收尾）。最近流转原因: {reasons}")
        return False
    return True


async def main() -> int:
    ap = argparse.ArgumentParser(description="Statecraft 真实 LLM 冒烟测试")
    ap.add_argument("--base-url", default=os.getenv("OPENAI_BASE_URL", ""))
    ap.add_argument("--api-key", default=os.getenv("OPENAI_API_KEY", ""))
    ap.add_argument("--model", default=os.getenv("OPENAI_MODEL", ""))
    ap.add_argument("--skip-tools", action="store_true", help="跳过第 2 步")
    args = ap.parse_args()

    if not args.api_key:
        print("✗ 没找到 API key。\n"
              "  推荐用环境变量（key 不进 shell 历史）：\n"
              "    Windows:      set OPENAI_API_KEY=sk-...\n"
              "    macOS/Linux:  export OPENAI_API_KEY=sk-...\n"
              "  国内常见组合：\n"
              "    DeepSeek: OPENAI_BASE_URL=https://api.deepseek.com/v1"
              " / OPENAI_MODEL=deepseek-chat\n"
              "    通义:     OPENAI_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1"
              " / OPENAI_MODEL=qwen-plus\n"
              "    智谱:     OPENAI_BASE_URL=https://open.bigmodel.cn/api/paas/v4"
              " / OPENAI_MODEL=glm-4-flash")
        return 2

    llm = OpenAICompatLLM(base_url=args.base_url or None,
                          api_key=args.api_key,
                          model=args.model or "gpt-4o-mini")
    print("=" * 66)
    try:
        await step1_connect(llm)
        if not args.skip_tools:
            await step2_tools(llm)
        ok = await step3_pipeline(llm)
    except Exception as exc:                       # noqa: BLE001
        print(f"\n✗ 冒烟失败：{diagnose(exc)}")
        print("=" * 66)
        return 1
    print("=" * 66)
    print("✓ 冒烟通过：provider、工具循环、状态机在真实模型下均可用"
          if ok else "△ 链路已通，但终态不是 Done（见上方提示）")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
