"""학습 회차 리포트 (#19) — 영역별 카드, 클릭 펼침 해설, 인라인 SVG.

단일 파일 HTML: 인라인 CSS만, **JS 없음**, 외부 요청 0, 전 출력 escape (#6).
클릭 펼침은 `<details>/<summary>`로 한다 — 스크립트가 필요 없고, CSP의
`script-src` 미포함(escape 구멍의 backstop)을 그대로 둘 수 있다.
"""
from __future__ import annotations

import html as _html

from ..quiz import Scorecard
from .charts import bar_chart, render_diagram

TEMPLATE_VERSION = "quiz-1"

_CSS = """
body{font-family:-apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo",sans-serif;
     max-width:880px;margin:2rem auto;padding:0 1rem;background:#fff;color:#111;
     line-height:1.6}
h1{border-bottom:2px solid #111;padding-bottom:.3rem;margin-bottom:.2rem}
h2{margin:2rem 0 .6rem;font-size:1.1rem;border-left:4px solid #111;padding-left:.5rem}
.sub{color:#666;font-size:.9rem;margin-top:0}
.score{font-size:2rem;font-weight:700}
.summary{border:1px solid #ddd;padding:1rem 1.2rem;margin:1.2rem 0}
.delta{display:inline-block;border:1px solid #111;padding:.1rem .5rem;margin-right:.4rem;
       font-size:.85rem}
details{border:1px solid #ddd;margin:.5rem 0;padding:.6rem .9rem}
details[open]{border-color:#111}
summary{cursor:pointer;font-weight:600}
summary::marker{color:#888}
.mark{font-weight:700;margin-right:.4rem}
.ok{color:#136c2e}.no{color:#a11}
.opt{margin:.15rem 0;padding-left:1.2rem}
.opt.correct{font-weight:700}
.body{margin-top:.8rem;padding-top:.8rem;border-top:1px dashed #ccc}
.evi{font-family:ui-monospace,Menlo,monospace;font-size:.8rem;color:#555}
table{border-collapse:collapse;width:100%;margin:.5rem 0}
td,th{border:1px solid #ccc;padding:.3rem .5rem;text-align:left;font-size:.85rem}
"""


def _e(v) -> str:
    return _html.escape(str(v), quote=True)


def _card(idx: int, r) -> str:
    q = r.question
    if r.choice is None:
        mark, cls, chose = "○", "no", "미응답"
    else:
        mark, cls = ("✓", "ok") if r.correct else ("✗", "no")
        chose = f"선택: {q.options[r.choice]}"
    opts = "".join(
        f'<div class="opt{" correct" if i == q.answer_index else ""}">'
        f'{_e(chr(65 + i))}) {_e(o)}{" ← 정답" if i == q.answer_index else ""}</div>'
        for i, o in enumerate(q.options))
    evi = "<br>".join(f"{_e(e.path)}:{e.start_line}–{e.end_line}" for e in q.evidence)
    diagram = render_diagram(q.diagram)
    return (
        f'<details><summary><span class="mark {cls}">{mark}</span>'
        f'{idx}. {_e(q.stem)}</summary>'
        f'<div class="body"><p class="sub">{_e(chose)}</p>{opts}'
        f'<p>{_e(q.explanation)}</p>{diagram}'
        f'<p class="evi">근거<br>{evi}</p></div></details>')


def render_quiz_report(card: Scorecard, *, repo: str | None,
                       added: int, cleared: int) -> str:
    """채점 결과 → 단일 파일 HTML."""
    pct = card.area_pct()
    chart = bar_chart(pct, unit="%") if pct else ""
    groups = []
    for area in dict.fromkeys(r.question.area for r in card.results):
        cards = "".join(_card(i + 1, r) for i, r in enumerate(card.results)
                        if r.question.area == area)
        ok, n = card.by_area.get(area, (0, 0))
        groups.append(f'<h2>{_e(area)} <span class="sub">{ok} / {n}</span></h2>{cards}')
    where = f' · <code>{_e(repo)}</code>' if repo else ""
    return f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>학습 회차 리포트</title><style>{_CSS}</style></head><body>
<h1>학습 회차 리포트</h1>
<p class="sub">문항을 클릭하면 해설이 열립니다{where}</p>
<div class="summary">
  <div class="score">{card.correct} / {card.total}</div>
  <p><span class="delta">오답 +{int(added)}</span><span class="delta">해소 {int(cleared)}</span></p>
  {chart}
</div>
{"".join(groups)}
</body></html>"""
