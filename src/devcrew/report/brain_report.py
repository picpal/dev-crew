"""Brain 응답 HTML 리포트 (#6 규약 준수: single-file, 인라인 CSS, JS 없음, 전 출력 escape).

디자인(2026-08-21 개편): 학습 회차 리포트(`quiz_report.py`)와 같은 토큰·같은 어휘를 쓴다.
라이트/다크는 자동 반전이 아니라 같은 토큰의 다른 값이고, `prefers-color-scheme`만 본다
(토글은 JS가 필요하다).

**폭은 눈이 아니라 숫자로 지킨다.** 모델이 쓰는 텍스트에는 줄바꿈 지점이 없는 100자짜리
브랜치명·커밋 SHA·URL이 예사로 들어온다. 그런 토큰 하나가 `overflow-wrap` 없는 블록에
들어가면 문서 전체가 가로로 늘어나 320px에서 본문이 화면 밖으로 나간다 — 가변 길이가
닿는 모든 곳에 `overflow-wrap:anywhere`를 건다.

**시각을 결정적으로 유지한다** — 생성 시각 같은 값을 넣지 않는다. 발행이 content hash로
멱등이라(§16.2) 렌더마다 값이 바뀌면 같은 리포트가 매번 새 객체가 된다.
"""
from __future__ import annotations

import hashlib
import html as _html
import re

TEMPLATE_VERSION = "brain-2"

_TOKENS = """
:root{color-scheme:light dark;
  --plane:#f9f9f7;--surface:#fcfcfb;--surface-2:#f2f1ed;
  --line:rgba(11,11,11,.10);--line-strong:#c3c2b7;
  --ink-1:#0b0b0b;--ink-2:#52514e;--ink-3:#898781;
  --series-1:#2a78d6;--warn:#a8620a;--shadow:rgba(11,11,11,.05);
  --code:rgba(11,11,11,.055)}
@media (prefers-color-scheme:dark){
  :root{--plane:#0d0d0d;--surface:#1a1a19;--surface-2:#212120;
    --line:rgba(255,255,255,.10);--line-strong:#383835;
    --ink-1:#fff;--ink-2:#c3c2b7;--ink-3:#898781;
    --series-1:#3987e5;--warn:#e0a33a;--shadow:rgba(0,0,0,.3);
    --code:rgba(255,255,255,.07)}}
"""

_CSS = _TOKENS + """
*{box-sizing:border-box}
body{margin:0;background:var(--plane);color:var(--ink-1);line-height:1.65;
  font-family:system-ui,-apple-system,"Apple SD Gothic Neo","Segoe UI",sans-serif;
  font-size:15px;-webkit-text-size-adjust:100%}
.page{max-width:52rem;margin:0 auto;
  padding:clamp(1.2rem,4vw,3rem) clamp(1rem,4vw,2rem) 4rem}
.masthead{padding-bottom:1.1rem;border-bottom:1px solid var(--line)}
.eyebrow{margin:0 0 .35rem;color:var(--ink-3);font-size:.75rem;letter-spacing:.04em}
h1{margin:0;font-size:clamp(1.3rem,3.2vw,1.75rem);font-weight:650;letter-spacing:-.01em;
  line-height:1.35;overflow-wrap:anywhere}
.where{display:flex;flex-wrap:wrap;align-items:center;gap:.4rem .7rem;
  margin:.7rem 0 0;color:var(--ink-2);font-size:.85rem;overflow-wrap:anywhere}
.where span{min-width:0}
.badge{flex:none;border:1px solid var(--line);border-radius:999px;
  background:var(--surface-2);color:var(--ink-2);
  padding:.1rem .6rem;font-size:.75rem;font-weight:600;white-space:nowrap}
.badge.is-pass{color:var(--series-1);border-color:var(--series-1)}
.badge.is-blocked{color:var(--warn);border-color:var(--warn)}
.lede{margin:1.6rem 0 0;color:var(--ink-2);font-size:.98rem;overflow-wrap:anywhere}
.sec{margin:2.2rem 0 0}
.sec-head{display:flex;align-items:baseline;justify-content:space-between;gap:.8rem;
  margin:0 0 .7rem;padding-bottom:.5rem;border-bottom:1px solid var(--line);
  font-size:1rem;font-weight:650}
.sec-name{min-width:0;overflow-wrap:anywhere}
.sec-count{flex:none;color:var(--ink-3);font-size:.8rem;font-weight:500;
  white-space:nowrap;font-variant-numeric:tabular-nums}
.items{margin:0;padding-left:1.2rem}
.items li{margin:.55rem 0;overflow-wrap:anywhere}
.items.marked{list-style:none;padding-left:0}
.items.marked li{border-left:2px solid var(--series-1);padding-left:.75rem}
.prose h2{margin:2rem 0 .6rem;padding-bottom:.4rem;border-bottom:1px solid var(--line);
  font-size:1rem;font-weight:650;overflow-wrap:anywhere}
.prose h3{margin:1.5rem 0 .4rem;font-size:.92rem;font-weight:650;
  color:var(--ink-2);overflow-wrap:anywhere}
.prose p{margin:.8rem 0;overflow-wrap:anywhere}
.prose ul{margin:.8rem 0;padding-left:1.2rem}
.prose li{margin:.35rem 0;overflow-wrap:anywhere}
.prose blockquote{margin:1rem 0;padding:.1rem 0 .1rem .9rem;
  border-left:2px solid var(--line-strong);color:var(--ink-2);overflow-wrap:anywhere}
.prose blockquote p{margin:.3rem 0}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.86em;
  background:var(--code);border-radius:4px;padding:0 .22em;overflow-wrap:anywhere}
pre{margin:1rem 0;background:var(--surface-2);border:1px solid var(--line);
  border-radius:10px;padding:.85rem 1rem;overflow-x:auto;font-size:.84em;
  line-height:1.55}
pre code{background:none;padding:0;font-size:1em;overflow-wrap:normal}
strong{font-weight:700}
.empty{margin:1.6rem 0 0;color:var(--ink-3);font-size:.9rem}
footer{margin-top:3rem;padding-top:1rem;border-top:1px solid var(--line);
  color:var(--ink-3);font-size:.75rem;overflow-wrap:anywhere}
"""


def _e(v) -> str:
    return _html.escape(str(v), quote=True)


def md_lite(text: str) -> str:
    """마크다운 부분집합 → HTML.

    **escape가 먼저, 태그 재구성은 그다음이다.** 순서가 뒤집히면 모델 출력이 태그가 되는
    주입 경로가 열린다 — 여기서 여는 태그는 우리가 쓴 것뿐이다.

    인용은 **연속한 `> ` 줄을 하나로 묶는다.** 줄마다 `<blockquote>`를 열면 한 문단짜리
    인용이 테두리 여러 개로 쪼개져 서로 다른 인용처럼 보인다.
    """
    out: list[str] = []
    in_ul = in_pre = in_bq = False

    def close_blocks(*, keep_ul: bool = False, keep_bq: bool = False) -> None:
        nonlocal in_ul, in_bq
        if in_ul and not keep_ul:
            out.append("</ul>")
            in_ul = False
        if in_bq and not keep_bq:
            out.append("</blockquote>")
            in_bq = False

    for raw in text.splitlines():
        if raw.strip().startswith("```"):
            if in_pre:
                out.append("</code></pre>")
            else:
                close_blocks()
                out.append("<pre><code>")
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
        is_bq = stripped.startswith("&gt;")
        close_blocks(keep_ul=is_li, keep_bq=is_bq)
        if stripped.startswith("### "):
            out.append(f"<h3>{stripped[4:]}</h3>")
        elif stripped.startswith("## "):
            out.append(f"<h2>{stripped[3:]}</h2>")
        elif stripped.startswith("# "):
            out.append(f"<h2>{stripped[2:]}</h2>")
        elif is_li:
            if not in_ul:
                out.append("<ul>")
                in_ul = True
            out.append(f"<li>{stripped[2:]}</li>")
        elif is_bq:
            if not in_bq:
                out.append("<blockquote>")
                in_bq = True
            out.append(f"<p>{stripped[5:] if stripped.startswith('&gt; ') else stripped[4:]}</p>")
        elif stripped:
            out.append(f"<p>{line}</p>")
    close_blocks()
    if in_pre:
        out.append("</code></pre>")
    # `<pre>`는 여는 태그 직후 개행 하나를 무시하지만 `<pre><code>`에서는 그 규칙이
    # `code`에 걸려 첫 줄이 빈 줄로 보인다. 이어 붙일 때 그 개행을 지운다.
    return ("\n".join(out).replace("<pre><code>\n", "<pre><code>")
            .replace("\n</code></pre>", "</code></pre>"))


def report_id(seed: str) -> str:
    return "brain-" + hashlib.sha256(seed.encode()).hexdigest()[:10]


def _page(title: str, eyebrow: str, where_html: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="ko" data-template-version="{TEMPLATE_VERSION}">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>{_e(title)}</title><style>{_CSS}</style></head>
<body>
<main class="page">
<header class="masthead">
  <p class="eyebrow">{_e(eyebrow)}</p>
  <h1>{_e(title)}</h1>
  <p class="where">{where_html}</p>
</header>
{body}
<footer>dev-crew · brain report · {TEMPLATE_VERSION}</footer>
</main>
</body></html>"""


def render_reply(*, topic: str, mode_hint: str, repo: str | None, text: str) -> str:
    """인터뷰 응답 리포트 — 답변 전문을 가독성 있게.

    주제는 제목으로만 낸다. 예전에는 `<h1>`과 바로 밑 메타 줄이 같은 문장을 두 번
    찍었다 — 두 번째는 정보가 아니라 소음이다.
    """
    where = ['<span class="badge">인터뷰</span>']
    if repo:
        where.append(f"<span><code>{_e(repo)}</code></span>")
    if mode_hint:
        where.append(f"<span>{_e(mode_hint)}</span>")
    body = md_lite(text) or '<p class="empty">아직 응답 본문이 없습니다.</p>'
    return _page(topic, "dev-crew · 인터뷰 응답", "".join(where),
                 f'<div class="prose">{body}</div>')


def _status_class(status: str) -> str:
    """모르는 status를 경고색으로 칠하지 않는다. 색은 아는 것에만 쓴다."""
    if status == "PASS":
        return " is-pass"
    if status == "BLOCKED":
        return " is-blocked"
    return ""


def render_brief(*, brief: dict, repo: str | None) -> str:
    """확정 brief 리포트 — 결정·수용 기준을 강조 색상으로."""
    status = str(brief.get("status") or "?")
    goal = str(brief.get("goal") or "")
    where = [f'<span class="badge{_status_class(status)}">{_e(status)}</span>']
    tr = brief.get("target_repo") or repo
    if tr:
        where.append(f"<span><code>{_e(tr)}</code></span>")

    secs: list[str] = []

    def sec(title: str, items, marked: bool = False) -> None:
        items = [i for i in (items or [])]
        if not items:
            return
        cls = "items marked" if marked else "items"
        li = "".join(f"<li>{_e(i)}</li>" for i in items)
        secs.append(
            f'<section class="sec"><h2 class="sec-head">'
            f'<span class="sec-name">{_e(title)}</span>'
            f'<span class="sec-count">{len(items)}</span></h2>'
            f'<ul class="{cls}">{li}</ul></section>')

    sec("결정 사항", brief.get("decisions"), marked=True)
    sec("제약", brief.get("constraints"))
    sec("수용 기준", brief.get("acceptance_criteria"), marked=True)
    sec("열린 질문", brief.get("open_questions"))

    summary = str(brief.get("summary") or "")
    lede = f'<p class="lede">{_e(summary)}</p>' if summary else ""
    body = lede + "".join(secs)
    if not body:
        body = '<p class="empty">기록된 항목이 없습니다.</p>'
    return _page(goal or "Brief", "dev-crew · 확정 brief", "".join(where), body)
