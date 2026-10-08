"""可观测性：JSONL 会话日志 + 活动流（P7，源自 Edict「可观测是第一优先级」）。

SessionLogger 把 flow / progress / 告警等统一追加写 JSONL（一行一事件）；
path=None 时只记内存（离线测试用）。activity_stream() 供看板/排查回放。
"""
import json
import os
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SessionLogger:
    def __init__(self, path: str | None = None):
        self.path = path
        self.records: list[dict] = []

    def log(self, kind: str, **data) -> None:
        rec = {"kind": kind, "at": _now(), **data}
        self.records.append(rec)
        if self.path:
            d = os.path.dirname(self.path)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def activity_stream(self, kinds: list[str] | None = None) -> list[dict]:
        """活动流：可按 kind 过滤（flow/progress/injection_warning/...）。"""
        if kinds is None:
            return list(self.records)
        return [r for r in self.records if r["kind"] in kinds]
