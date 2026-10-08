"""黄金集数据结构与加载（JSONL，# 开头的行视为注释跳过）。"""
import json
from dataclasses import dataclass, field
from pathlib import Path

GOLDEN_DIR = Path(__file__).parent / "golden"


@dataclass
class GoldenL1:
    """L1 检索样本：query + 语料 + 相关条目下标。"""
    query: str
    corpus: list[str]
    relevant: list[int]


@dataclass
class GoldenL2:
    """L2 审核样本：待审文本 + 期望判定（approve / reject）。"""
    text: str
    expected: str


@dataclass
class GoldenL3:
    """L3 端到端样本：任务 + MockLLM 脚本（离线回归）+ 期望终态 + 必经状态。"""
    description: str
    script: dict
    expected_state: str
    must_visit: list[str] = field(default_factory=list)


def load_jsonl(path: Path) -> list[dict]:
    cases = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                cases.append(json.loads(line))
    return cases


def load_l1(path: Path | None = None) -> list[GoldenL1]:
    return [GoldenL1(**c) for c in load_jsonl(path or GOLDEN_DIR / "l1_memory.jsonl")]


def load_l2(path: Path | None = None) -> list[GoldenL2]:
    return [GoldenL2(**c) for c in load_jsonl(path or GOLDEN_DIR / "l2_review.jsonl")]


def load_l3(path: Path | None = None) -> list[GoldenL3]:
    return [GoldenL3(**c) for c in load_jsonl(path or GOLDEN_DIR / "l3_tasks.jsonl")]
