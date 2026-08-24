"""코드 실행 리포트 — 좌: 코드 / 우: 스텝별 이유와 변수 상태.

**JS 없이 돈다.** `worker/src/index.js`의 CSP는 `default-src 'none'`이라 스크립트가
아예 실행되지 않는다. 그래서 2초 자동 진행과 수동 스텝이 전부 CSS로 서 있다 —
`@keyframes` + 스텝별 `animation-delay`, 그리고 라디오 + `:checked ~`.
JS 제어(스크럽·배속)는 나중에 얹는 progressive enhancement이고, 없어도 페이지는
완결이어야 한다.

스텝 i는 `[i*2s, (i+1)*2s)` 동안만 켜진다. 양수 delay + `infinite`를 쓰면 시작 전에는
애니메이션이 적용되지 않아 기본 스타일(꺼짐)이 그대로 보이고, 한 바퀴가 끝나면 다시
같은 창으로 돌아온다 — 그래서 기본 스타일은 반드시 '꺼짐'이어야 한다.
"""
from __future__ import annotations

import html as _html

from .quiz_report import TOKENS

STEP_SECONDS = 2
VISIBLE_ROWS = 24          # 코드 창에 한 번에 보이는 줄 수 (넘으면 따라 스크롤한다)
LEAD_ROWS = 8              # 활성 줄을 창의 이 위치쯤에 둔다


def _e(v) -> str:
    return _html.escape(str(v), quote=True)


def _scroll_row(row: int, total: int) -> int:
    """활성 줄이 row일 때 코드 창을 몇 줄 내릴까. 끝에서 빈 공간이 뜨지 않게 자른다."""
    if total <= VISIBLE_ROWS:
        return 0
    return max(0, min(row - LEAD_ROWS, total - VISIBLE_ROWS))


def _keyframes(n: int, rows: list[int], total_rows: int) -> str:
    """스텝 창 + 코드 자동 스크롤 keyframes.

    창 폭은 전체의 1/n이다. 경계에서 두 스텝이 함께 켜지지 않도록 아주 작은 간격을 둔다.
    """
    pct = 100.0 / n
    edge = min(pct / 2, 0.01)
    frames = [f"@keyframes win{{0%,{pct - edge:.4f}%{{opacity:1}}"
              f"{pct:.4f}%,100%{{opacity:0}}}}"]
    if total_rows > VISIBLE_ROWS:
        stops = []
        for i, row in enumerate(rows):
            a, b = i * pct, (i + 1) * pct - edge
            y = _scroll_row(row, total_rows)
            stops.append(f"{a:.4f}%,{b:.4f}%{{transform:translateY(calc(-1*{y}*var(--lh)))}}")
        frames.append("@keyframes scroll{" + "".join(stops) + "}")
    return "".join(frames)


_CSS = TOKENS + """
*{box-sizing:border-box}
body{margin:0;background:var(--plane);color:var(--ink-1);
  font:15px/1.6 -apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo",Pretendard,
  "Segoe UI",Roboto,sans-serif}
.page{max-width:1180px;margin:0 auto;padding:28px 20px 56px}
.masthead{margin:0 0 18px}
.eyebrow{margin:0;font-size:.76rem;letter-spacing:.08em;text-transform:uppercase;
  color:var(--ink-3)}
h1{margin:.25em 0 .2em;font-size:1.5rem;line-height:1.3}
.where{margin:0;color:var(--ink-3);font-size:.88rem}
.card{background:var(--surface);border:1px solid var(--line);border-radius:12px;
  padding:16px 18px;margin:0 0 16px;box-shadow:var(--shadow)}
.role p{margin:0}
.q{margin:0;color:var(--ink-2);font-size:.92rem}
.note{margin:10px 0 0;font-size:.84rem;color:var(--ink-3)}

/* ── 재생 제어 (라디오는 :checked ~ 를 쓰려고 .stage 앞에 둔다) ───────────── */
.rt{position:absolute;opacity:0;pointer-events:none;width:0;height:0}
.bar{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:0 0 14px}
.bar .hint{font-size:.82rem;color:var(--ink-3);margin-right:6px}
.dot{display:inline-flex;align-items:center;justify-content:center;min-width:26px;
  height:26px;padding:0 7px;border:1px solid var(--line);border-radius:7px;
  background:var(--surface);color:var(--ink-2);font-size:.78rem;cursor:pointer;
  font-variant-numeric:tabular-nums}
.dot:hover{border-color:var(--line-strong)}

/* ── 무대 ────────────────────────────────────────────────────────────────── */
.stage{display:grid;grid-template-columns:minmax(0,1.15fr) minmax(0,1fr);gap:16px;
  align-items:start}
@media (max-width:860px){.stage{grid-template-columns:minmax(0,1fr)}}

.code{--lh:1.55rem;position:relative;overflow:hidden;background:var(--surface);
  border:1px solid var(--line);border-radius:12px;padding:14px 0;
  max-height:calc(24*var(--lh) + 28px)}
.track{position:relative;animation:none}
.row{display:grid;grid-template-columns:3.6rem 1fr;height:var(--lh);align-items:center;
  font:.82rem/var(--lh) ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  white-space:pre;position:relative;z-index:1}
.no{color:var(--ink-3);text-align:right;padding-right:.9rem;user-select:none;
  font-variant-numeric:tabular-nums}
.src{padding-right:1rem;overflow-x:auto;color:var(--ink-1)}
.hl{position:absolute;left:0;right:0;height:var(--lh);z-index:0;opacity:0;
  background:color-mix(in srgb,var(--good) 16%,transparent);
  border-left:3px solid var(--good)}

.side{display:grid}
.step{grid-area:1/1;opacity:0;background:var(--surface);border:1px solid var(--line);
  border-radius:12px;padding:16px 18px;box-shadow:var(--shadow)}
.st-head{display:flex;gap:10px;align-items:baseline;margin:0 0 10px}
.st-n{font-size:.78rem;color:var(--ink-3);font-variant-numeric:tabular-nums}
.st-line{margin-left:auto;font:.78rem ui-monospace,Menlo,monospace;color:var(--ink-3)}
.st-why{margin:0 0 14px;font-size:.98rem;line-height:1.65}
.vars{display:grid;grid-template-columns:auto minmax(0,1fr);gap:4px 12px;margin:0;
  font:.83rem/1.6 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.vk{color:var(--ink-3)}
.vv{color:var(--ink-1);overflow-wrap:anywhere}
.is-changed .vk,.is-changed .vv{color:var(--good);font-weight:600}
.is-changed .vv::after{content:" ←";opacity:.7}
.novars{margin:0;font-size:.84rem;color:var(--ink-3)}
"""

_AUTOPLAY = """
.hl{animation:win var(--total) linear infinite;animation-delay:var(--at)}
.step{animation:win var(--total) linear infinite;animation-delay:var(--at)}
.track{animation:scroll var(--total) linear infinite}
/* 손으로 한 칸이라도 누르면 자동 재생을 멈춘다 — 읽는 중에 화면이 넘어가면 안 된다 */
body:has(.rt:checked) .hl,body:has(.rt:checked) .step,
body:has(.rt:checked) .track{animation:none}
"""


def render_code_report(trace, *, question: str, repo: str | None = None) -> str:
    """실행 추적 → 단일 파일 HTML. 스크립트 없음, 외부 참조 없음."""
    steps = list(trace.steps)
    n = len(steps)
    lines = list(trace.lines)
    first = lines[0].number if lines else 1
    rows = [s.line - first for s in steps]
    total = f"{n * STEP_SECONDS}s"

    code_rows = "".join(
        f'<div class="row"><span class="no">{ln.number}</span>'
        f'<span class="src">{_e(ln.text) or "&nbsp;"}</span></div>' for ln in lines)
    hls = "".join(
        f'<div class="hl hl{i}" style="--at:{i * STEP_SECONDS}s;'
        f'top:calc({r}*var(--lh))"></div>' for i, r in enumerate(rows))

    panels = []
    for i, s in enumerate(steps):
        if s.vars:
            body = '<dl class="vars">' + "".join(
                f'<div class="v{" is-changed" if v.changed else ""}" '
                f'style="display:contents">'
                f'<dt class="vk">{_e(v.name)}</dt><dd class="vv">{_e(v.value)}</dd></div>'
                for v in s.vars) + "</dl>"
        else:
            body = '<p class="novars">이 스텝에서는 상태가 바뀌지 않습니다.</p>'
        panels.append(
            f'<section class="step st{i}" style="--at:{i * STEP_SECONDS}s">'
            f'<div class="st-head"><span class="st-n">step {i + 1} / {n}</span>'
            f'<span class="st-line">L{s.line}</span></div>'
            f'<p class="st-why">{_e(s.reason)}</p>{body}</section>')

    radios = "".join(f'<input class="rt" type="radio" name="st" id="st{i}">'
                     for i in range(n))
    dots = "".join(f'<label class="dot" for="st{i}">{i + 1}</label>' for i in range(n))
    # 수동 선택 시 그 스텝만 켜고 코드도 그 줄로 옮긴다 (자동 재생은 위에서 꺼진다).
    # 짚는 자리는 **명시 클래스**로 한다 — `nth-of-type`은 태그 기준이라 같은 태그가
    # 섞이는 순간 엉뚱한 것을 가리킨다.
    manual = "".join(
        f'#st{i}:checked~.stage .st{i},#st{i}:checked~.stage .hl{i}{{opacity:1}}'
        f'#st{i}:checked~.stage .track{{transform:translateY(calc(-1*'
        f'{_scroll_row(rows[i], len(lines))}*var(--lh)))}}' for i in range(n))
    dropped = (f'<p class="note">스텝 {trace.dropped}건은 표시 범위 밖을 가리켜 '
               f'제외했습니다.</p>' if trace.dropped else "")
    where = " · ".join(x for x in (_e(repo) if repo else "", _e(trace.path)) if x)

    return f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>{_e(trace.title)}</title>
<style>{_CSS}{_keyframes(n, rows, len(lines))}{_AUTOPLAY}{manual}
.stage{{--total:{total}}}</style></head><body>
<main class="page">
<header class="masthead">
  <p class="eyebrow">dev-crew · 코드 실행 리포트</p>
  <h1>{_e(trace.title)}</h1>
  <p class="where">{where}</p>
</header>
<section class="card role">
  <p>{_e(trace.role_of_code)}</p>
  <p class="note">질문: {_e(question)}</p>
</section>
{radios}
<div class="bar"><span class="hint">{STEP_SECONDS}초마다 한 칸 · 번호를 누르면 멈추고
그 스텝으로 (다시 재생하려면 새로고침)</span>{dots}</div>
<div class="stage">
  <div class="code"><div class="track">{hls}{code_rows}</div></div>
  <div class="side">{"".join(panels)}</div>
</div>
{dropped}
</main>
</body></html>"""
