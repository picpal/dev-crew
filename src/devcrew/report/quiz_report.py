"""학습 회차 리포트 (#19) — 요약 대시보드, 영역별 카드, 클릭 펼침 해설.

단일 파일 HTML: 인라인 CSS만, **JS 없음**, 외부 요청 0, 전 출력 escape (#6).
클릭 펼침은 `<details>/<summary>`로 한다 — 스크립트가 필요 없고, CSP의
`script-src` 미포함(escape 구멍의 backstop)을 그대로 둘 수 있다.

디자인(2026-08-21 개편): 그림은 SVG 고정 좌표가 아니라 HTML+CSS다. 한글 라벨이
고정폭 칸을 넘어 밖으로 새던 문제가 여기서 나왔다 — HTML 텍스트는 접히고 막대는
컨테이너를 따라 줄어든다. 색은 토큰 하나로 모으고 다크 모드는 같은 토큰을 갈아끼운다.
`prefers-color-scheme`만 쓴다(토글은 JS가 필요하다).

**시각을 결정적으로 유지한다** — 생성 시각 같은 값을 넣지 않는다. 발행이
content hash로 멱등이라(§16.2) 렌더마다 값이 바뀌면 같은 회차가 매번 새 객체가 된다.
"""
from __future__ import annotations

import html as _html
import re

from ..quiz import Scorecard
from .charts import CHART_CSS, bar_chart, render_diagram

TEMPLATE_VERSION = "quiz-2"

TOKENS = """
:root{color-scheme:light dark;
  --plane:#f9f9f7;--surface:#fcfcfb;--surface-2:#f2f1ed;
  --line:rgba(11,11,11,.10);--line-strong:#c3c2b7;--grid:#e1e0d9;
  --ink-1:#0b0b0b;--ink-2:#52514e;--ink-3:#898781;
  --series-1:#2a78d6;--good:#006300;--bad:#d03b3b;--shadow:rgba(11,11,11,.05);
  --code:rgba(11,11,11,.055)}
@media (prefers-color-scheme:dark){
  :root{--plane:#0d0d0d;--surface:#1a1a19;--surface-2:#212120;
    --line:rgba(255,255,255,.10);--line-strong:#383835;--grid:#2c2c2a;
    --ink-1:#fff;--ink-2:#c3c2b7;--ink-3:#898781;
    --series-1:#3987e5;--good:#0ca30c;--bad:#e66767;--shadow:rgba(0,0,0,.3);
    --code:rgba(255,255,255,.07)}}
"""

_CSS = TOKENS + """
*{box-sizing:border-box}
body{margin:0;background:var(--plane);color:var(--ink-1);line-height:1.65;
  font-family:system-ui,-apple-system,"Apple SD Gothic Neo","Segoe UI",sans-serif;
  -webkit-text-size-adjust:100%}
.page{max-width:56rem;margin:0 auto;padding:clamp(1.2rem,4vw,3rem) clamp(1rem,4vw,2rem) 5rem}
.masthead{padding-bottom:1.1rem;border-bottom:1px solid var(--line)}
.eyebrow{margin:0 0 .35rem;color:var(--ink-3);font-size:.75rem;letter-spacing:.04em}
h1{margin:0;font-size:clamp(1.35rem,3.4vw,1.8rem);font-weight:650;letter-spacing:-.01em}
.masthead .where{margin:.4rem 0 0;color:var(--ink-2);font-size:.85rem;
  overflow-wrap:anywhere}
.card{background:var(--surface);border:1px solid var(--line);border-radius:14px;
  box-shadow:0 1px 2px var(--shadow)}
.hero{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.15fr);gap:1.6rem;
  align-items:center;padding:1.5rem clamp(1rem,3vw,1.8rem);margin:1.6rem 0 1rem}
.hero-label{margin:0;color:var(--ink-3);font-size:.75rem;letter-spacing:.04em}
.hero-figure{margin:.15rem 0 .1rem;font-size:clamp(2.6rem,9vw,3.6rem);font-weight:600;
  line-height:1.05;letter-spacing:-.03em}
.hero-of{color:var(--ink-3);font-size:.42em;font-weight:500;margin-left:.15em}
.hero-sub{margin:.5rem 0 0;color:var(--ink-2);font-size:.85rem}
.meter{height:8px;border-radius:999px;background:var(--grid);margin-top:.85rem;
  overflow:hidden}
.meter-fill{height:100%;border-radius:0 4px 4px 0;background:var(--series-1);min-width:2px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(5.5rem,1fr));
  gap:.6rem;margin:0}
.stat{border:1px solid var(--line);border-radius:10px;background:var(--surface-2);
  padding:.7rem .8rem;min-width:0}
.stat dt{margin:0;color:var(--ink-3);font-size:.72rem;line-height:1.35;
  overflow-wrap:anywhere}
.stat dd{margin:.2rem 0 0;font-size:1.25rem;font-weight:600;letter-spacing:-.01em}
.panel{padding:1.2rem clamp(1rem,3vw,1.8rem) 1.4rem;margin:1rem 0 2.2rem}
.panel-title{margin:0 0 .2rem;font-size:.95rem;font-weight:650}
.panel-note{margin:.9rem 0 0;padding-top:.8rem;border-top:1px solid var(--line);
  color:var(--ink-2);font-size:.82rem}
.panel-note b{color:var(--ink-1)}
.tableview{margin-top:.9rem}
.tableview>summary{color:var(--ink-3);font-size:.75rem;cursor:pointer}
.area{margin:2.2rem 0 0}
.area-head{display:flex;align-items:baseline;justify-content:space-between;gap:.8rem;
  margin:0 0 .7rem;padding-bottom:.5rem;border-bottom:1px solid var(--line);
  font-size:1rem;font-weight:650}
.area-name{min-width:0;overflow-wrap:anywhere}
.area-score{color:var(--ink-3);font-size:.8rem;font-weight:500;white-space:nowrap;
  font-variant-numeric:tabular-nums}
.qa{margin:.55rem 0;border:1px solid var(--line);border-radius:12px;
  background:var(--surface);overflow:hidden}
.qa[open]{border-color:var(--line-strong)}
.qa>summary{display:flex;gap:.6rem;align-items:flex-start;padding:.85rem 1rem;
  cursor:pointer;list-style:none}
.qa>summary::-webkit-details-marker{display:none}
.qa>summary::after{content:"▾";color:var(--ink-3);margin-left:.2rem;font-size:.7rem;
  line-height:1.9;flex:none}
.qa[open]>summary::after{content:"▴"}
.qa>summary:hover{background:var(--surface-2)}
.q-no{color:var(--ink-3);font-size:.78rem;font-variant-numeric:tabular-nums;
  padding-top:.15rem;white-space:nowrap}
.q-stem{flex:1;min-width:0;overflow-wrap:anywhere;font-weight:550;font-size:.92rem}
.chip{display:inline-flex;align-items:center;gap:.25rem;border-radius:999px;
  padding:.1rem .5rem;font-size:.72rem;font-weight:600;white-space:nowrap;
  border:1px solid var(--line);background:var(--surface-2);color:var(--ink-2)}
.chip-ok{color:var(--good)}
.chip-no{color:var(--bad)}
.qa-body{padding:0 1rem 1.1rem;border-top:1px solid var(--line)}
.opts{list-style:none;margin:1rem 0 0;padding:0;display:grid;gap:.35rem}
.opt{display:grid;grid-template-columns:1.4rem minmax(0,1fr);gap:.5rem;
  align-items:start;border:1px solid transparent;border-radius:9px;
  padding:.45rem .6rem;font-size:.86rem;background:var(--surface-2)}
.opt.is-answer{border-color:var(--good)}
.opt.is-picked{border-color:var(--bad)}
.opt-key{color:var(--ink-3);font-weight:600;font-size:.78rem;padding-top:.1rem}
.opt-text{min-width:0;overflow-wrap:anywhere;display:block}
.opt-tags{display:flex;flex-wrap:wrap;gap:.3rem;margin-top:.3rem}
.sect{margin-top:1.2rem}
.sect-title{margin:0 0 .35rem;color:var(--ink-3);font-size:.74rem;letter-spacing:.04em;
  font-weight:600}
.sect p{margin:0;font-size:.88rem;color:var(--ink-2)}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.86em;
  background:var(--code);border-radius:4px;padding:0 .22em;overflow-wrap:anywhere}
.q-stem strong,.opt-text strong{font-weight:700}
.evi{margin:.35rem 0 0;padding:.55rem .7rem;border-left:2px solid var(--line-strong);
  background:var(--surface-2);border-radius:0 8px 8px 0}
.evi-at{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.74rem;
  color:var(--ink-3);overflow-wrap:anywhere}
.evi-quote{margin:.25rem 0 0;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
  font-size:.76rem;color:var(--ink-2);white-space:pre-wrap;overflow-wrap:anywhere}
.evi-quote.is-prose{font-family:inherit;font-size:.83rem;line-height:1.6}
@media (max-width:640px){.hero{grid-template-columns:minmax(0,1fr);gap:1.2rem}}
""" + CHART_CSS


_CODE_RE = re.compile(r"`([^`\n]+)`")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.S)


def _e(v) -> str:
    return _html.escape(str(v), quote=True)


def _rich(v) -> str:
    """모델이 쓴 인라인 마크다운(굵게·코드)만 살려 낸다.

    **escape가 먼저, 변환은 그다음이다.** 순서가 뒤집히면 모델 출력이 태그가 되는
    주입 경로가 열린다 — 여기서 여는 태그는 우리가 쓴 두 종류(strong·code)뿐이다.
    안 바꾸면 지문에 별표가 그대로 찍힌다 (사용자 피드백 2026-08-21)."""
    out = _CODE_RE.sub(r"<code>\1</code>", _e(v))
    return _BOLD_RE.sub(r"<strong>\1</strong>", out)


PROSE_SUFFIX = (".md", ".txt", ".rst", ".adoc")


def _prose(path: str) -> str:
    """문서에서 뽑은 인용인가. 코드가 아니면 monospace를 씌우지 않는다 —
    한글 산문을 고정폭으로 깔면 글자가 성기고 줄이 길어져 읽기가 나빠진다."""
    return " is-prose" if str(path).lower().endswith(PROSE_SUFFIX) else ""


def _mark(r) -> tuple[str, str]:
    """정오 표시 — 색만으로 말하지 않는다. 기호 + 말이 함께 간다."""
    if r.choice is None:
        return "", "○ 미응답"
    return ("chip-ok", "✓ 정답") if r.correct else ("chip-no", "✗ 오답")


def _option(i: int, o: str, *, is_answer: bool, is_picked: bool) -> str:
    tags = []
    if is_answer:
        tags.append('<span class="chip chip-ok">✓ 정답</span>')
    if is_picked and not is_answer:
        tags.append('<span class="chip chip-no">✗ 내 선택</span>')
    if is_picked and is_answer:
        tags.append('<span class="chip">내 선택</span>')
    cls = " is-answer" if is_answer else (" is-picked" if is_picked else "")
    tag_html = f'<span class="opt-tags">{"".join(tags)}</span>' if tags else ""
    return (f'<li class="opt{cls}"><span class="opt-key">{chr(65 + i)}</span>'
            f'<span class="opt-text">{_rich(o)}{tag_html}</span></li>')


def _card(idx: int, r, *, open_wrong: bool) -> str:
    """문항 하나. **틀린 문항은 펼친 채로 낸다** — 리포트를 여는 이유가 거기 있는데
    전부 접어 두면 사용자가 열 문항을 일일이 눌러야 오답을 찾는다.

    다만 절반 넘게 틀린 회차에서는 전부 접는다(`open_wrong=False`). 열 개 중 아홉이
    펼쳐져 있으면 펼침은 "여기를 보라"는 뜻을 잃고 페이지만 길어진다."""
    q = r.question
    cls, label = _mark(r)
    opts = "".join(_option(i, o, is_answer=(i == q.answer_index), is_picked=(i == r.choice))
                   for i, o in enumerate(q.options))
    evi = "".join(
        f'<div class="evi"><div class="evi-at">{_e(e.path)}:{e.start_line}–{e.end_line}</div>'
        f'<div class="evi-quote{_prose(e.path)}">{_e(e.quote)}</div></div>'
        for e in q.evidence)
    diagram = render_diagram(q.diagram)
    return (
        f'<details class="qa"{" open" if (open_wrong and not r.correct) else ""}>'
        f'<summary><span class="q-no">Q{idx}</span>'
        f'<span class="q-stem">{_rich(q.stem)}</span>'
        f'<span class="chip {cls}">{label}</span></summary>'
        f'<div class="qa-body"><ul class="opts">{opts}</ul>'
        f'<div class="sect"><p class="sect-title">해설</p><p>{_rich(q.explanation)}</p>'
        f'{diagram}</div>'
        f'<div class="sect"><p class="sect-title">근거</p>{evi}</div>'
        f'</div></details>')


def _area_table(card: Scorecard) -> str:
    """차트의 표 짝 — 막대 길이로만 읽히는 값이 없게 한다."""
    rows = "".join(
        f"<tr><td>{_e(area)}</td><td>{ok}</td><td>{n}</td>"
        f"<td>{round(ok * 100 / n) if n else 0}%</td></tr>"
        for area, (ok, n) in card.by_area.items())
    return ('<details class="tableview"><summary>표로 보기</summary>'
            '<div class="table-wrap"><table><thead><tr><th>영역</th><th>정답</th>'
            f'<th>문항</th><th>정답률</th></tr></thead><tbody>{rows}</tbody></table>'
            "</div></details>")


def _weakest(card: Scorecard) -> str:
    """가장 약한 영역 한 곳만 짚는다. 막대마다 값을 달면 아무것도 안 읽힌다."""
    ranked = sorted(((ok / n, area, ok, n) for area, (ok, n) in card.by_area.items()
                     if n), key=lambda t: (t[0], t[1]))
    if not ranked or ranked[0][0] >= 1:
        return '<p class="panel-note">모든 영역을 다 맞혔습니다.</p>'
    _, area, ok, n = ranked[0]
    return (f'<p class="panel-note">가장 약한 영역 — <b>{_e(area)}</b> {ok} / {n}</p>')


def render_quiz_report(card: Scorecard, *, repo: str | None,
                       added: int, cleared: int) -> str:
    """채점 결과 → 단일 파일 HTML."""
    # 막대 하나짜리 차트는 차트가 아니다. "가장 약한 영역"도 비교가 있어야 뜻이 선다.
    # 만점 회차도 마찬가지 — 100% 막대만 늘어놓느니 hero 한 줄이 낫다.
    show_areas = len(card.by_area) > 1 and card.correct < card.total
    pct = card.area_pct() if show_areas else []
    chart = bar_chart(pct, unit="%", full=100.0) if pct else ""
    table = _area_table(card) if pct else ""
    wrong = card.total - card.correct
    open_wrong = 0 < wrong <= card.total / 2
    groups = []
    for area in dict.fromkeys(r.question.area for r in card.results):
        cards = "".join(_card(i + 1, r, open_wrong=open_wrong)
                        for i, r in enumerate(card.results)
                        if r.question.area == area)
        ok, n = card.by_area.get(area, (0, 0))
        groups.append(
            f'<section class="area"><h2 class="area-head">'
            f'<span class="area-name">{_e(area)}</span>'
            f'<span class="area-score">{ok} / {n}</span></h2>{cards}</section>')
    where = (f'<p class="where">대상 저장소 <code>{_e(repo)}</code></p>' if repo else "")
    rate = round(card.correct * 100 / card.total) if card.total else 0
    # 제목이 늘 "무엇을 놓쳤나"면 만점 회차에서 거짓말이 된다
    headline = ("이번 회차, 전부 맞혔습니다" if card.total and card.correct == card.total
                else "이번 회차에서 무엇을 놓쳤나")
    delta = f"+{int(added)}" if added else "0"
    panel = (f'<section class="card panel"><h2 class="panel-title">영역별 정답률</h2>'
             f'{chart}{table}{_weakest(card)}</section>') if pct else ""
    return f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>학습 회차 리포트</title><style>{_CSS}</style></head><body>
<main class="page">
<header class="masthead">
  <p class="eyebrow">dev-crew · 학습 회차 리포트</p>
  <h1>{headline}</h1>
  {where}
</header>
<section class="card hero">
  <div>
    <p class="hero-label">정답</p>
    <p class="hero-figure">{card.correct}<span class="hero-of">/ {card.total}</span></p>
    <p class="hero-sub">{card.total}문항 중 {card.correct}문항 정답 · {rate}%</p>
    <div class="meter"><div class="meter-fill" style="width:{rate}%"></div></div>
  </div>
  <dl class="stats">
    <div class="stat"><dt>오답 노트 추가</dt><dd>{delta}</dd></div>
    <div class="stat"><dt>해소</dt><dd>{int(cleared)}</dd></div>
    <div class="stat"><dt>영역</dt><dd>{len(card.by_area)}</dd></div>
  </dl>
</section>
{panel}
{"".join(groups)}
</main>
</body></html>"""
