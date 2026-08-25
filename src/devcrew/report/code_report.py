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
import re

from .quiz_report import TOKENS

STEP_SECONDS = 2
VISIBLE_ROWS = 24          # 코드 창에 한 번에 보이는 줄 수 (넘으면 따라 스크롤한다)
LEAD_ROWS = 8              # 활성 줄을 창의 이 위치쯤에 둔다


def _e(v) -> str:
    return _html.escape(str(v), quote=True)


_CODE_RE = re.compile(r"`([^`\n]+)`")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.S)
# 모델이 별 하나로 쓰는 경우도 받는다. `**이중**`은 앞뒤 lookaround로 건드리지 않는다.
_SINGLE_BOLD_RE = re.compile(r"(?<![*\w])\*([^*\n]+)\*(?![*\w])")
_SENT_RE = re.compile(r"(?<=[.!?다])\s+")


def _md(v) -> str:
    """모델이 쓴 인라인 마크다운만 살린다 — **escape가 먼저다.**

    순서가 뒤집히면 모델 출력이 태그가 되는 주입 경로가 열린다. 여기서 여는 태그는
    우리가 쓴 두 종류(code·strong)뿐이다. 안 바꾸면 `**굵게**`와 백틱이 날문자로 찍힌다
    (사용자 2026-08-25).
    """
    out = _CODE_RE.sub(r"<code>\1</code>", _e(v))
    out = _BOLD_RE.sub(r"<strong>\1</strong>", out)
    return _SINGLE_BOLD_RE.sub(r"<strong>\1</strong>", out)


def _lead(text: str, limit: int = 140) -> tuple[str, str]:
    """(첫 문장, 나머지). 상단 설명이 벽이면 아무도 안 읽는다 — 첫 문장만 펴 둔다."""
    body = (text or "").strip()
    if len(body) <= limit:
        return body, ""
    head = _SENT_RE.split(body, maxsplit=1)
    if len(head) == 2 and len(head[0]) <= limit * 2:
        return head[0], head[1]
    # 쓸 만한 문장 경계가 없으면 **접힌 쪽에 전문을 둔다**(앞부분이 겹친다). 글자 수로
    # 자르면 `**굵게**`나 백틱 한가운데가 잘려 두 조각 모두 마크다운이 깨진다 —
    # 조금 겹치는 편이 깨지는 것보다 낫다.
    return body[:limit].rstrip() + "…", body


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
.page{max-width:1240px;margin:0 auto;padding:26px 20px 56px}
.eyebrow{margin:0;font-size:.74rem;letter-spacing:.08em;text-transform:uppercase;
  color:var(--ink-3)}
h1{margin:.25em 0 .18em;font-size:1.38rem;line-height:1.32}
.where{margin:0 0 14px;color:var(--ink-3);font-size:.82rem;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace;overflow-wrap:anywhere}
.q{margin:0 0 14px;padding:9px 13px;border-left:3px solid var(--line-strong);
  background:var(--surface);border-radius:0 8px 8px 0;font-size:.9rem;color:var(--ink-2)}
.q b{color:var(--ink-3);font-weight:600;font-size:.78rem;letter-spacing:.04em}

/* 상단 역할 설명 — 첫 문장만 펴 두고 나머지는 접는다 (벽이면 아무도 안 읽는다) */
.role{margin:0 0 14px;background:var(--surface);border:1px solid var(--line);
  border-radius:10px;padding:12px 15px;font-size:.92rem;line-height:1.7}
.role>summary{cursor:pointer;list-style:none;color:var(--ink-1)}
.role>summary::-webkit-details-marker{display:none}
.role>summary::after{content:" 더 보기";color:var(--ink-3);font-size:.8rem;
  white-space:nowrap}
.role[open]>summary::after{content:" 접기"}
.role .rest{margin:.55em 0 0;color:var(--ink-2)}
.role.is-short>summary::after{content:""}
.role code,.st-why code,.q code{font:.86em ui-monospace,SFMono-Regular,Menlo,monospace;
  background:var(--code);padding:.1em .35em;border-radius:4px}

/* ── 안내 + 재생 (라디오는 :checked ~ 를 쓰려고 .stage 앞에 둔다) ─────────── */
.rt,.auto{position:absolute;opacity:0;pointer-events:none;width:0;height:0}
.bar{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin:0 0 12px;
  font-size:.84rem;color:var(--ink-3)}
.bar .replay{display:inline-flex;align-items:center;gap:5px;padding:5px 11px;
  border:1px solid var(--line);border-radius:7px;background:var(--surface);
  color:var(--ink-2);cursor:pointer}
.bar .replay:hover{border-color:var(--line-strong);color:var(--ink-1)}
/* JS가 붙으면 CSS 재생 대신 JS가 몬다 — 그래서 컨트롤도 서로 배타적으로 보인다.
   스크립트가 CSP에 막히거나 실패해도 아래 `.nojs`가 그대로 남아 페이지는 완결이다. */
.jsonly{display:none}
.js .jsonly{display:inline-flex}
.js .nojs{display:none}
.bar button{font:inherit;font-size:.84rem}
.bar .pos{font-variant-numeric:tabular-nums;color:var(--ink-2);min-width:3.6em;
  text-align:center}
.play .ico::before{content:"▶"}
.js[data-play="1"] .play .ico::before{content:"⏸"}
.keys{color:var(--ink-3);font-size:.78rem}

/* ── 무대 ────────────────────────────────────────────────────────────────── */
.stage{display:grid;grid-template-columns:minmax(0,1.2fr) minmax(0,1fr);gap:16px;
  align-items:start}
@media (max-width:900px){.stage{grid-template-columns:minmax(0,1fr)}}

/* **세로 이동 방식이 모드마다 다르다.** JS가 없으면 `translateY`로 따라가야 하므로
   컨테이너는 스크롤하지 않는다(`overflow:hidden`). JS가 붙으면 진짜 `scrollTop`으로
   움직이고 transform은 끈다 — 둘을 같이 켜면 콘텐츠는 transform으로 올라가 있는데
   스크롤 컨테이너는 그걸 모르므로 **위로 되돌아갈 수 없다**(2026-08-25 회귀). */
.code{--lh:1.55rem;position:relative;overflow:hidden;background:var(--surface);
  border:1px solid var(--line);border-radius:12px;padding:14px 0;
  max-height:calc(24*var(--lh) + 28px)}
/* `scroll-behavior:smooth`를 쓰지 않는다 — 켜 두면 스크립트가 거는 스크롤이
   **아예 적용되지 않는다**(scrollTop 대입도, scrollTo({behavior:'smooth'})도
   최종값이 0으로 남는 것을 실측했다, 2026-08-25). 2초에 한 칸이라 즉시 이동으로
   충분하고, 무엇보다 확실하게 동작한다. */
.js .code{overflow:auto}
.track{position:relative;min-width:max-content;transition:transform .22s ease}
.js .track{transform:none}
.row{display:grid;grid-template-columns:3.4rem 1fr;height:var(--lh);align-items:center;
  font:.82rem/var(--lh) ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  white-space:pre;position:relative;z-index:1;margin:0}
.no{color:var(--ink-3);text-align:right;padding-right:.9rem;user-select:none;
  font-variant-numeric:tabular-nums}
.src{padding-right:1.4rem;color:var(--ink-1)}
/* 스텝이 있는 줄만 누를 수 있다 — 눌러도 아무 일 없는 것을 눌러 보이게 하지 않는다 */
.is-step{cursor:pointer}
.is-step .no{color:var(--good);font-weight:600}
.is-step:hover{background:color-mix(in srgb,var(--good) 8%,transparent)}
.hl{position:absolute;left:0;right:0;height:var(--lh);z-index:0;opacity:0;
  background:color-mix(in srgb,var(--good) 16%,transparent);
  border-left:3px solid var(--good)}

.side{display:grid;position:sticky;top:16px}
.step{grid-area:1/1;opacity:0;background:var(--surface);border:1px solid var(--line);
  border-radius:12px;padding:16px 18px;box-shadow:var(--shadow)}
.st-head{display:flex;gap:10px;align-items:baseline;margin:0 0 10px}
.st-n{font-size:.78rem;color:var(--ink-3);font-variant-numeric:tabular-nums}
.st-line{margin-left:auto;font:.78rem ui-monospace,Menlo,monospace;color:var(--ink-3)}
.st-why{margin:0 0 14px;font-size:.97rem;line-height:1.7}
.visits{margin:0 0 12px;font-size:.79rem;color:var(--ink-3);display:flex;
  flex-wrap:wrap;gap:6px;align-items:center}
.visits label{cursor:pointer;padding:1px 7px;border:1px solid var(--line);
  border-radius:5px;color:var(--ink-2)}
.visits label:hover{border-color:var(--line-strong)}
.visits .now{border-color:var(--good);color:var(--good)}
.vars{display:grid;grid-template-columns:auto minmax(0,1fr);gap:4px 12px;margin:0;
  font:.83rem/1.6 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.vk{color:var(--ink-3)}
.vv{color:var(--ink-1);overflow-wrap:anywhere}
.is-changed .vk,.is-changed .vv{color:var(--good);font-weight:600}
.is-changed .vv::after{content:" ←";opacity:.7}
.novars{margin:0;font-size:.84rem;color:var(--ink-3)}
.note{margin:12px 0 0;font-size:.83rem;color:var(--ink-3)}
.fig{margin:0 0 14px;background:var(--surface);border:1px solid var(--line);
  border-radius:10px;padding:12px}
.fig svg{display:block;width:100%;height:auto;max-width:100%}
"""


# ── JS 제어 레이어 (progressive enhancement) ────────────────────────────────
# **이 문자열은 고정이다.** worker CSP가 `script-src 'sha256-…'`로 이 내용의 해시만
# 허용하므로, 리포트마다 달라지면 해시가 어긋나 스크립트가 통째로 차단된다. 리포트별
# 값(스텝 간격)은 스크립트에 박지 않고 DOM(`data-step`)에서 읽는다.
# `tests/test_code_report.py::test_worker_csp_matches_the_script_hash`가 worker의
# 해시와 대조한다 — 어긋나면 조용히 죽지 않고 테스트가 먼저 깨진다.
#
# 없어도 페이지는 완결이다. CSS가 자동 재생·라인 클릭을 이미 하고, 이 스크립트는
# 같은 라디오를 대신 켜서 재생/일시정지·이전/다음·키보드를 얹을 뿐이다.
JS_SRC = """(function(){
var d=document,r=[].slice.call(d.querySelectorAll('.rt'));
if(!r.length||!d.body.classList)return;
d.documentElement.className+=' js';
var i=0,on=false,t=null,ms=(parseFloat(d.body.getAttribute('data-step'))||2)*1000;
var pos=d.getElementById('pos');
function sync(){if(pos)pos.textContent=(i+1)+' / '+r.length;}
function see(k){
var pane=d.querySelector('.code'),el=d.querySelector('.hl'+k);
if(!pane||!el)return;
var pr=pane.getBoundingClientRect(),er=el.getBoundingClientRect(),m=er.height*3;
if(er.top<pr.top+m)pane.scrollTop-=(pr.top+m-er.top);
else if(er.bottom>pr.bottom-m)pane.scrollTop+=(er.bottom-(pr.bottom-m));
}
function go(k){i=(k%r.length+r.length)%r.length;r[i].checked=true;sync();see(i);}
function stop(){on=false;d.documentElement.setAttribute('data-play','0');
clearInterval(t);t=null;}
function play(){on=true;d.documentElement.setAttribute('data-play','1');
clearInterval(t);t=setInterval(function(){go(i+1);},ms);}
d.addEventListener('click',function(e){
var n=e.target,a=null;
while(n&&n!==d){if(n.getAttribute&&n.getAttribute('data-act')){a=n;break;}n=n.parentNode;}
if(!a)return;
var k=a.getAttribute('data-act');
if(k==='play'){on?stop():play();}
else if(k==='next'){stop();go(i+1);}
else if(k==='prev'){stop();go(i-1);}
else if(k==='restart'){go(0);play();}
});
d.addEventListener('change',function(e){
var x=r.indexOf(e.target);if(x<0)return;i=x;stop();sync();see(i);
});
d.addEventListener('keydown',function(e){
if(e.metaKey||e.ctrlKey||e.altKey)return;
if(e.key==='ArrowRight'){e.preventDefault();stop();go(i+1);}
else if(e.key==='ArrowLeft'){e.preventDefault();stop();go(i-1);}
else if(e.key===' '||e.key==='Spacebar'){e.preventDefault();on?stop():play();}
});
go(0);play();
})();"""


def script_hash() -> str:
    """CSP `script-src`에 넣을 `sha256-<base64>`. worker와 대조하는 값이다."""
    import base64
    import hashlib
    return "sha256-" + base64.b64encode(
        hashlib.sha256(JS_SRC.encode()).digest()).decode()

_AUTOPLAY = """
.hl{animation:win var(--total) linear infinite;animation-delay:var(--at)}
.step{animation:win var(--total) linear infinite;animation-delay:var(--at)}
.track{animation:scroll var(--total) linear infinite}
/* 줄을 하나라도 누르면 자동 재생을 멈춘다 — 읽는 중에 화면이 넘어가면 안 된다.
   '처음부터' 라디오는 `.rt`가 아니라서 여기 걸리지 않고, 같은 name 그룹이라 누르는
   순간 스텝 선택이 풀려 애니메이션이 처음부터 다시 돈다. */
body:has(.rt:checked) .hl,body:has(.rt:checked) .step,
body:has(.rt:checked) .track{animation:none}
"""


def render_code_report(trace, *, question: str, repo: str | None = None,
                       diagram: str | None = None) -> str:
    """실행 추적 → 단일 파일 HTML. 스크립트 없음, 외부 참조 없음."""
    steps = list(trace.steps)
    n = len(steps)
    lines = list(trace.lines)
    first = lines[0].number if lines else 1
    rows = [s.line - first for s in steps]
    # 같은 줄을 여러 번 지나는 경우(루프)를 미리 모은다 — 라벨 하나로는 한 회차밖에
    # 못 가리키므로, 패널에서 다른 회차로 건너뛸 수 있게 해야 한다.
    visits: dict[int, list[int]] = {}
    for i, st in enumerate(steps):
        visits.setdefault(st.line, []).append(i)

    code_rows = []
    for ln in lines:
        src = f'<span class="no">{ln.number}</span><span class="src">{_e(ln.text) or "&nbsp;"}</span>'
        idxs = visits.get(ln.number)
        if idxs:
            code_rows.append(f'<label class="row is-step" for="st{idxs[0]}">{src}</label>')
        else:
            code_rows.append(f'<div class="row">{src}</div>')
    hls = "".join(
        f'<div class="hl hl{i}" style="--at:{i * STEP_SECONDS}s;'
        f'top:calc({r}*var(--lh))"></div>' for i, r in enumerate(rows))

    panels = []
    for i, st in enumerate(steps):
        if st.vars:
            body = '<dl class="vars">' + "".join(
                f'<div class="v{" is-changed" if v.changed else ""}" style="display:contents">'
                f'<dt class="vk">{_e(v.name)}</dt><dd class="vv">{_e(v.value)}</dd></div>'
                for v in st.vars) + "</dl>"
        else:
            body = '<p class="novars">이 스텝에서는 상태가 바뀌지 않습니다.</p>'
        same = visits.get(st.line, [])
        jump = ""
        if len(same) > 1:
            chips = "".join(
                f'<label class="{"now" if j == i else ""}" for="st{j}">{k + 1}회차</label>'
                for k, j in enumerate(same))
            jump = (f'<p class="visits">이 줄은 {len(same)}번 지나갑니다{chips}</p>')
        panels.append(
            f'<section class="step st{i}" style="--at:{i * STEP_SECONDS}s">'
            f'<div class="st-head"><span class="st-n">step {i + 1} / {n}</span>'
            f'<span class="st-line">L{st.line}</span></div>'
            f'<p class="st-why">{_md(st.reason)}</p>{jump}{body}</section>')

    radios = "".join(f'<input class="rt" type="radio" name="st" id="st{i}">'
                     for i in range(n))
    manual = "".join(
        f'#st{i}:checked~.stage .st{i},#st{i}:checked~.stage .hl{i}{{opacity:1}}'
        f'html:not(.js) #st{i}:checked~.stage .track{{transform:translateY(calc(-1*'
        f'{_scroll_row(rows[i], len(lines))}*var(--lh)))}}' for i in range(n))

    lead, rest = _lead(trace.role_of_code)
    more = f'<p class="rest">{_md(rest)}</p>' if rest else ""
    role = (f'<details class="role{"" if rest else " is-short"}">'
            f'<summary>{_md(lead)}</summary>{more}</details>')
    # 이미 살균된 SVG만 온다 (`tutor_vis.extract_svg`) — 그대로 넣는다.
    fig = f'<figure class="fig">{diagram}</figure>' if diagram else ""
    dropped = (f'<p class="note">스텝 {trace.dropped}건은 표시 범위 밖을 가리켜 '
               f'제외했습니다.</p>' if trace.dropped else "")
    where = " · ".join(x for x in (_e(repo) if repo else "", _e(trace.path)) if x)

    return f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>{_e(trace.title)}</title>
<style>{_CSS}{_keyframes(n, rows, len(lines))}{_AUTOPLAY}{manual}
.stage{{--total:{n * STEP_SECONDS}s}}</style></head><body data-step="{STEP_SECONDS}">
<main class="page">
<p class="eyebrow">dev-crew · 코드 실행 리포트</p>
<h1>{_e(trace.title)}</h1>
<p class="where">{where}</p>
<p class="q"><b>질문</b><br>{_md(question)}</p>
{role}
{fig}
<input class="auto" type="radio" name="st" id="stAuto">
{radios}
<div class="bar">
<label class="replay nojs" for="stAuto">▶ 처음부터</label>
<button class="replay jsonly play" type="button" data-act="play"
 aria-label="재생/일시정지"><span class="ico"></span></button>
<button class="replay jsonly" type="button" data-act="prev">←</button>
<span class="replay jsonly pos" id="pos"></span>
<button class="replay jsonly" type="button" data-act="next">→</button>
<button class="replay jsonly" type="button" data-act="restart">처음부터</button>
<span>{STEP_SECONDS}초마다 한 칸씩 넘어갑니다 · 초록색 줄번호를 누르면 그 줄의 설명이
뜹니다</span><span class="keys jsonly">← → 스텝 · space 재생/멈춤</span></div>
<div class="stage">
  <div class="code"><div class="track">{hls}{"".join(code_rows)}</div></div>
  <div class="side">{"".join(panels)}</div>
</div>
{dropped}
</main>
<script>{JS_SRC}</script>
</body></html>"""
