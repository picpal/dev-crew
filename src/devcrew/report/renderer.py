"""single-file HTML Report (#6) — 인라인 CSS만, 전 출력 escape, JS 없음."""
from __future__ import annotations

import html as _html

TEMPLATE_VERSION = "poc-1"

_CSS = """
body{font-family:sans-serif;max-width:900px;margin:2rem auto;padding:0 1rem;
     background:#fff;color:#111}
table{border-collapse:collapse;width:100%}
td,th{border:1px solid #ccc;padding:.4rem .6rem;text-align:left}
h1,h2{border-bottom:2px solid #111;padding-bottom:.2rem}
.badge{display:inline-block;padding:.1rem .5rem;border:1px solid #111}
"""


def _e(v) -> str:
    return _html.escape(str(v), quote=True)


def render(view: dict) -> str:
    inst_rows = "".join(
        f"<tr><td>{_e(i['instance_id'])}</td><td>{_e(i['role'])}</td>"
        f"<td>{_e(i['model'])}</td><td>{_e(i['effort'])}</td></tr>"
        for i in view.get("instances", []))
    loop_rows = "".join(
        f"<tr><td>{_e(l['iteration'])}</td><td>{_e(l['verdict'])}</td></tr>"
        for l in view.get("loops", []))
    decisions = "".join(f"<li>{_e(d)}</li>" for d in view.get("decisions", []))
    return f"""<!doctype html>
<html lang="ko" data-template-version="{TEMPLATE_VERSION}">
<head><meta charset="utf-8"><title>{_e(view['task_id'])} Report</title>
<style>{_CSS}</style></head>
<body>
<h1>{_e(view['task_id'])} <span class="badge">{_e(view['status'])}</span></h1>
<p>Lead time: {_e(view.get('lead_time_min', '?'))}m</p>
<h2>Agent Instances</h2>
<table><tr><th>ID</th><th>Role</th><th>Model</th><th>Effort</th></tr>{inst_rows}</table>
<h2>Review Loops</h2>
<table><tr><th>#</th><th>Verdict</th></tr>{loop_rows}</table>
<h2>Decisions</h2><ul>{decisions}</ul>
</body></html>"""
