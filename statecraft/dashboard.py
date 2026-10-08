"""零依赖静态看板（P7 收尾）：Task 列表 + SessionLogger → 单个 HTML 文件。

纯字符串拼接，无任何第三方依赖，浏览器直接打开即可。
用法：
    from statecraft.dashboard import write_dashboard
    write_dashboard("board.html", tasks, logger)

看板内容：
- 顶部统计条：任务状态分布 + 活动事件计数
- 任务卡片：状态徽章 / 委派来源 / 流转时间线（from→to+原因）/ 进展上报
- 告警区：注入检测告警 + Saga 补偿记录（含失败）
"""
from html import escape
from pathlib import Path

from .core.state_machine import Task

_STATE_COLOR = {
    "Created": "#6b7280", "Planning": "#7c5cd6", "Reviewing": "#0e9488",
    "Executing": "#2563eb", "PendingConfirm": "#d97706",
    "Done": "#16a34a", "Blocked": "#dc2626", "Cancelled": "#9ca3af",
}

_CSS = """
body{font-family:"Microsoft YaHei",system-ui,sans-serif;margin:0;background:#f5f6f8;color:#1f2328}
.wrap{max-width:960px;margin:0 auto;padding:24px 16px}
h1{font-size:20px;margin:0 0 4px}
.muted{color:#8a8f98;font-size:12px}
.chips{display:flex;gap:8px;flex-wrap:wrap;margin:16px 0}
.chip{background:#fff;border-radius:6px;padding:6px 12px;font-size:13px;box-shadow:0 1px 2px rgba(0,0,0,.06)}
.chip b{font-size:16px;margin-left:4px}
.card{background:#fff;border-radius:8px;padding:14px 16px;margin-bottom:14px;box-shadow:0 1px 3px rgba(0,0,0,.07)}
.badge{display:inline-block;color:#fff;border-radius:10px;padding:2px 10px;font-size:12px}
.tid{font-weight:700;margin-right:8px}
h3{margin:0 0 8px;font-size:15px;display:flex;align-items:center;gap:8px}
ul{margin:4px 0 10px;padding-left:20px;font-size:13px;line-height:1.7}
.sec{font-size:12px;color:#8a8f98;margin-top:6px}
.warn{background:#fef2f2;border-left:3px solid #dc2626;padding:8px 12px;border-radius:4px;font-size:13px;margin-bottom:8px}
.ok{color:#16a34a}.bad{color:#dc2626}
"""


def _chip(label: str, count: int, color: str) -> str:
    return (f'<span class="chip">{escape(label)}'
            f'<b style="color:{color}">{count}</b></span>')


def _task_card(t: Task) -> str:
    color = _STATE_COLOR.get(t.state, "#6b7280")
    flows = "".join(
        f"<li><code>{escape(f['from'])}</code> → <b>{escape(f['to'])}</b>"
        f" <span class='muted'>（{escape(str(f['reason']))}）</span></li>"
        for f in t.flow_log) or "<li class='muted'>尚无流转</li>"
    progs = "".join(
        f"<li><b>{escape(p['agent'])}</b>：{escape(p['text'])}</li>"
        for p in t.progress_log) or "<li class='muted'>尚无进展上报</li>"
    parent = (f" · 委派自 <code>{escape(t.parent_id)}</code>"
              if t.parent_id else "")
    reason = (f"<div class='sec'>阻塞原因：{escape(str(t.blocked_reason))}</div>"
              if t.blocked_reason else "")
    return (f'<div class="card"><h3>'
            f'<span class="tid">{escape(t.id)}</span>'
            f'<span class="badge" style="background:{color}">{escape(t.state)}</span>'
            f'{parent}</h3>'
            f'<div>{escape(t.description)}</div>{reason}'
            f'<div class="sec">流转时间线</div><ul>{flows}</ul>'
            f'<div class="sec">进展上报</div><ul>{progs}</ul></div>')


def render_dashboard(tasks, logger=None, title: str = "AgentCore 看板") -> str:
    """渲染为完整 HTML 字符串。"""
    tasks = list(tasks)
    records = logger.activity_stream() if logger is not None else []

    state_counts: dict[str, int] = {}
    for t in tasks:
        state_counts[t.state] = state_counts.get(t.state, 0) + 1
    kind_counts: dict[str, int] = {}
    for r in records:
        kind_counts[r["kind"]] = kind_counts.get(r["kind"], 0) + 1

    chips = " ".join(_chip(s, n, _STATE_COLOR.get(s, "#6b7280"))
                     for s, n in sorted(state_counts.items()))
    chips += _chip("活动事件", len(records), "#2563eb")

    kind_rows = "".join(
        f"<li>{escape(k)}：<b>{n}</b></li>"
        for k, n in sorted(kind_counts.items())) or "<li class='muted'>无</li>"

    warns: list[str] = []
    for r in records:
        if r["kind"] == "injection_warning":
            warns.append(f'<div class="warn">注入告警 {escape(str(r.get("task_id")))}：'
                         f'命中 {escape(str(r.get("patterns")))}</div>')
        if r["kind"] == "saga_compensate":
            cls = "ok" if r.get("status") == "ok" else "bad"
            warns.append(
                f'<div class="warn">Saga 补偿 {escape(str(r.get("task_id")))} '
                f'{escape(str(r.get("tool")))}：<span class="{cls}">'
                f'{escape(str(r.get("status")))}</span> '
                f'<span class="muted">{escape(str(r.get("detail") or r.get("reason")))}</span></div>')
    warn_html = "".join(warns) or "<p class='muted'>无告警</p>"

    cards = "".join(_task_card(t) for t in tasks) or "<p class='muted'>暂无任务</p>"
    return (f"<!DOCTYPE html><html lang='zh'><head><meta charset='utf-8'>"
            f"<title>{escape(title)}</title><style>{_CSS}</style></head>"
            f"<body><div class='wrap'><h1>{escape(title)}</h1>"
            f"<div class='muted'>静态快照 · 由 statecraft 生成</div>"
            f"<div class='chips'>{chips}</div>"
            f"<div class='card'><h3>活动统计（按事件类型）</h3><ul>{kind_rows}</ul></div>"
            f"<div class='card'><h3>告警 / 补偿</h3>{warn_html}</div>"
            f"{cards}</div></body></html>")


def write_dashboard(path: str, tasks, logger=None,
                    title: str = "AgentCore 看板") -> str:
    """渲染并写文件，返回文件路径。"""
    p = Path(path)
    if p.parent and str(p.parent):
        p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(render_dashboard(tasks, logger, title), encoding="utf-8")
    return str(p)
