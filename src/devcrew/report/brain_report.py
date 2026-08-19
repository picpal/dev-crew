"""Brain 응답 HTML 리포트 (#6 규약 준수: single-file, 인라인 CSS, JS 없음, 전 출력 escape).

디자인: 모노톤 기본(잉크/그레이), 강조만 색상 — status 배지·결정 하이라이트에 한정.
"""
from __future__ import annotations

import hashlib
import html as _html
import re

TEMPLATE_VERSION = "brain-1"

_CSS = """
:root{--ink:#1a1a1a;--sub:#666;--line:#e2e2e2;--bg:#fdfdfd;--card:#fff;
      --accent:#2f6fdb;--warn:#b7791f}
*{box-sizing:border-box}
body{font-family:-apple-system,'Apple SD Gothic Neo','Noto Sans KR',sans-serif;
     max-width:820px;margin:0 auto;padding:3rem 1.5rem;background:var(--bg);
     color:var(--ink);line-height:1.7;font-size:15px}
header{border-bottom:2px solid var(--ink);padding-bottom:1rem;margin-bottom:2rem}
h1{font-size:1.5rem;margin:0 0 .4rem}
.meta{color:var(--sub);font-size:.85rem}
.meta b{color:var(--ink);font-weight:600}
.badge{display:inline-block;padding:.1rem .6rem;border-radius:3px;font-size:.8rem;
       font-weight:600;border:1px solid var(--line);color:var(--sub)}
.badge.pass{color:#fff;background:var(--accent);border-color:var(--accent)}
.badge.blocked{color:#fff;background:var(--warn);border-color:var(--warn)}
h2{font-size:1.05rem;margin:2rem 0 .6rem;padding-bottom:.3rem;
   border-bottom:1px solid var(--line)}
ul{padding-left:1.3rem;margin:.4rem 0}
li{margin:.25rem 0}
li.hl{border-left:3px solid var(--accent);padding-left:.6rem;list-style:none;
      margin-left:-1.3rem}
code{background:#f0f0f0;border:1px solid var(--line);border-radius:3px;
     padding:.05rem .35rem;font-size:.85em;font-family:ui-monospace,Menlo,monospace}
pre{background:#f7f7f7;border:1px solid var(--line);border-radius:4px;
    padding:.8rem 1rem;overflow-x:auto;font-size:.85em}
blockquote{border-left:3px solid var(--line);margin:.6rem 0;padding:.1rem 1rem;
           color:var(--sub)}
strong{font-weight:700}
footer{margin-top:3rem;padding-top:1rem;border-top:1px solid var(--line);
       color:var(--sub);font-size:.78rem}
"""


def _e(v) -> str:
    return _html.escape(str(v), quote=True)


def md_lite(text: str) -> str:
    """마크다운 부분집합 → HTML. 전체 escape 후 안전한 태그만 재구성 (JS/원시 HTML 불가)."""
    out: list[str] = []
    in_ul = in_pre = False
    for raw in text.splitlines():
        if raw.strip().startswith("```"):
            if in_pre:
                out.append("</pre>")
            else:
                if in_ul:
                    out.append("</ul>")
                    in_ul = False
                out.append("<pre>")
            in_pre = not in_pre
            continue
        if in_pre:
            out.append(_e(raw))
            continue
        line = _e(raw)
        line = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", line)
        line = re.sub(r"`([^`]+)`", r"<code>\1</code>", line)
        stripped = line.strip()
        is_li = stripped.startswith(("- ", "• ", "* "))
        if in_ul and not is_li:
            out.append("</ul>")
            in_ul = False
        if stripped.startswith("### "):
            out.append(f"<h2>{stripped[4:]}</h2>")
        elif stripped.startswith("## "):
            out.append(f"<h2>{stripped[3:]}</h2>")
        elif stripped.startswith("# "):
            out.append(f"<h2>{stripped[2:]}</h2>")
        elif is_li:
            if not in_ul:
                out.append("<ul>")
                in_ul = True
            out.append(f"<li>{stripped[2:]}</li>")
        elif stripped.startswith("&gt; "):
            out.append(f"<blockquote>{stripped[5:]}</blockquote>")
        elif stripped:
            out.append(f"<p>{line}</p>")
    if in_ul:
        out.append("</ul>")
    if in_pre:
        out.append("</pre>")
    return "\n".join(out)


def report_id(seed: str) -> str:
    return "brain-" + hashlib.sha256(seed.encode()).hexdigest()[:10]


def _page(title: str, badge_html: str, meta: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="ko" data-template-version="{TEMPLATE_VERSION}">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(title)}</title><style>{_CSS}</style></head>
<body>
<header><h1>{_e(title)} {badge_html}</h1><div class="meta">{meta}</div></header>
{body}
<footer>dev-crew · brain report · {TEMPLATE_VERSION}</footer>
</body></html>"""


def render_reply(*, topic: str, mode_hint: str, repo: str | None, text: str) -> str:
    """인터뷰 응답 리포트 — 답변 전문을 가독성 있게."""
    meta = f"주제: <b>{_e(topic)}</b>"
    if repo:
        meta += f" · repo: <b>{_e(repo)}</b>"
    meta += f" · {_e(mode_hint)}"
    return _page(topic, '<span class="badge">인터뷰</span>', meta, md_lite(text))


def render_brief(*, brief: dict, repo: str | None) -> str:
    """확정 brief 리포트 — 결정·수용 기준을 강조 색상으로."""
    status = brief.get("status", "?")
    cls = "pass" if status == "PASS" else "blocked"
    meta = f"목표: <b>{_e(brief.get('goal', ''))}</b>"
    tr = brief.get("target_repo") or repo
    if tr:
        meta += f" · repo: <b>{_e(tr)}</b>"
    secs: list[str] = []

    def sec(title: str, items: list, hl: bool = False):
        if not items:
            return
        li = "".join(f'<li{" class=\"hl\"" if hl else ""}>{_e(i)}</li>' for i in items)
        secs.append(f"<h2>{_e(title)}</h2><ul>{li}</ul>")

    sec("결정 사항", brief.get("decisions", []), hl=True)
    sec("제약", brief.get("constraints", []))
    sec("수용 기준", brief.get("acceptance_criteria", []), hl=True)
    sec("열린 질문", brief.get("open_questions", []))
    body = f"<p>{_e(brief.get('summary', ''))}</p>" + "".join(secs)
    return _page(brief.get("goal") or "Brief",
                 f'<span class="badge {cls}">{_e(status)}</span>', meta, body)
