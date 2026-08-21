"""워크플로 실행 리포트 (#6) — 인라인 CSS만, 전 출력 escape, JS 없음.

단일 파일 HTML: 외부 요청 0, strict CSP(`default-src 'none'`) 아래서 그대로 뜬다.

디자인(2026-08-21 개편): 원본 템플릿은 실물로 열어 본 적이 없는 코드였다. 실측으로
드러난 것 — 375px에서 문서 폭이 657px까지 밀렸고(표의 안 끊기는 instance_id + 긴
status 배지), viewport meta가 없어 모바일은 980px를 축소해 그렸고, 배경이 `#fff`로
박혀 다크 OS에서 흰 판때기였다. 그래서 quiz_report와 같은 토큰 팔레트를 쓰고, 표는
전부 `overflow-x:auto` 컨테이너에 넣고, 가변 길이 값에는 `overflow-wrap:anywhere`를
건다. 다크는 자동 반전이 아니라 같은 토큰의 별도 값이다.

**시각을 결정적으로 유지한다** — 생성 시각 같은 값을 넣지 않는다. 발행이 content
hash로 멱등이라 렌더마다 값이 바뀌면 같은 실행이 매번 새 객체가 된다.
"""
from __future__ import annotations

import html as _html

TEMPLATE_VERSION = "poc-2"

_TOKENS = """
:root{color-scheme:light dark;
  --plane:#f9f9f7;--surface:#fcfcfb;--surface-2:#f2f1ed;
  --line:rgba(11,11,11,.10);--line-strong:#c3c2b7;
  --ink-1:#0b0b0b;--ink-2:#52514e;--ink-3:#898781;
  --good:#006300;--bad:#d03b3b;--shadow:rgba(11,11,11,.05);
  --code:rgba(11,11,11,.055)}
@media (prefers-color-scheme:dark){
  :root{--plane:#0d0d0d;--surface:#1a1a19;--surface-2:#212120;
    --line:rgba(255,255,255,.10);--line-strong:#383835;
    --ink-1:#fff;--ink-2:#c3c2b7;--ink-3:#898781;
    --good:#0ca30c;--bad:#e66767;--shadow:rgba(0,0,0,.3);
    --code:rgba(255,255,255,.07)}}
"""

_CSS = _TOKENS + """
*{box-sizing:border-box}
body{margin:0;background:var(--plane);color:var(--ink-1);line-height:1.65;
  font-family:system-ui,-apple-system,"Apple SD Gothic Neo","Segoe UI",sans-serif;
  -webkit-text-size-adjust:100%}
.page{max-width:56rem;margin:0 auto;
  padding:clamp(1.2rem,4vw,3rem) clamp(1rem,4vw,2rem) 5rem}
.masthead{padding-bottom:1.1rem;border-bottom:1px solid var(--line)}
.eyebrow{margin:0 0 .35rem;color:var(--ink-3);font-size:.75rem;letter-spacing:.04em}
h1{margin:0;font-size:clamp(1.2rem,3.2vw,1.65rem);font-weight:650;letter-spacing:-.01em;
  overflow-wrap:anywhere;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
/* 알약이 아니라 둥근 사각이다 — status는 두 줄을 넘기도 하는데(모델이 쓰는 문자열),
   양끝이 반원인 상자에 두 줄이 들어가면 모양이 무너진다. */
.status{display:inline-block;margin:.7rem 0 0;max-width:100%;border-radius:10px;
  padding:.2rem .7rem;font-size:.8rem;font-weight:600;overflow-wrap:anywhere;
  border:1px solid var(--line-strong);background:var(--surface-2);color:var(--ink-2)}
.status-ok{color:var(--good)}
.status-no{color:var(--bad)}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(6.5rem,1fr));
  gap:.6rem;margin:1.4rem 0 0}
.stat{border:1px solid var(--line);border-radius:10px;background:var(--surface-2);
  padding:.7rem .8rem;min-width:0}
.stat dt{margin:0;color:var(--ink-3);font-size:.72rem;line-height:1.35;
  overflow-wrap:anywhere}
.stat dd{margin:.2rem 0 0;font-size:1.2rem;font-weight:600;letter-spacing:-.01em;
  font-variant-numeric:tabular-nums}
.stat dd.is-none{font-size:.9rem;font-weight:500;color:var(--ink-3);padding:.2rem 0}
.stat .unit{color:var(--ink-3);font-size:.62em;font-weight:500;margin-left:.15em}
.panel{background:var(--surface);border:1px solid var(--line);border-radius:14px;
  box-shadow:0 1px 2px var(--shadow);
  padding:1.2rem clamp(1rem,3vw,1.6rem) 1.3rem;margin:1.6rem 0 0}
.panel-title{display:flex;align-items:baseline;justify-content:space-between;gap:.8rem;
  margin:0 0 .9rem;font-size:.95rem;font-weight:650}
.panel-count{color:var(--ink-3);font-size:.78rem;font-weight:500;white-space:nowrap;
  font-variant-numeric:tabular-nums}
.table-wrap{overflow-x:auto;border:1px solid var(--line);border-radius:10px}
table{border-collapse:collapse;width:100%;font-size:.84rem}
/* 좁은 화면에서 열을 짓이기지 않는다 — 대신 컨테이너가 옆으로 구른다.
   min-width가 없으면 375px에서 instance_id가 8글자씩 12줄로 접혀 표가 못 읽힌다. */
.t-inst{min-width:33rem}
th,td{padding:.5rem .75rem;text-align:left;vertical-align:top;
  border-bottom:1px solid var(--line);overflow-wrap:anywhere}
thead th{background:var(--surface-2);color:var(--ink-3);font-size:.73rem;
  font-weight:600;white-space:nowrap}
tbody tr:last-child td{border-bottom:0}
.c-num{width:3rem;color:var(--ink-3);font-variant-numeric:tabular-nums;
  white-space:nowrap}
.c-id{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.92em}
.c-model{color:var(--ink-2)}
/* 짧은 열거값이 `mediu/m`처럼 두 줄로 찢기지 않게 한다. 한글 role은 단어 안에서
   끊지 않는다(keep-all) — 표가 넓어지면 컨테이너가 구르면 된다. */
.c-role{word-break:keep-all}
.c-effort{white-space:nowrap}
.verdict{font-weight:600}
.verdict-ok{color:var(--good)}
.verdict-no{color:var(--bad)}
.decisions{list-style:none;margin:0;padding:0;display:grid;gap:.5rem;counter-reset:d}
.decisions li{counter-increment:d;display:grid;
  grid-template-columns:1.7rem minmax(0,1fr);gap:.3rem;align-items:start;
  border:1px solid var(--line);border-radius:10px;background:var(--surface-2);
  padding:.6rem .8rem;font-size:.88rem;overflow-wrap:anywhere}
.decisions li::before{content:counter(d);color:var(--ink-3);font-size:.76rem;
  font-weight:600;font-variant-numeric:tabular-nums;padding-top:.18rem}
.empty{margin:0;color:var(--ink-3);font-size:.84rem}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.86em;
  background:var(--code);border-radius:4px;padding:0 .22em;overflow-wrap:anywhere}
"""

_OK_WORDS = ("PASS", "DONE", "OK", "APPROVED", "COMPLETE")
_NO_WORDS = ("NOT_PASS", "FAIL", "ABORT", "BLOCKED", "ERROR", "TIMEOUT", "EXCEEDED")


def _e(v) -> str:
    return _html.escape(str(v), quote=True)


def _tone(v, ok_suffix: str, no_suffix: str) -> str:
    """상태 문자열의 색조. **색만으로 말하지 않는다** — 값 자체가 항상 글자로 보이고,
    색은 거들 뿐이다. 판정 못 하면 중립으로 둔다(agent가 쓴 임의 문자열일 수 있다)."""
    up = str(v).upper()
    if any(w in up for w in _NO_WORDS):    # NOT_PASS가 PASS보다 먼저 걸려야 한다
        return no_suffix
    return ok_suffix if any(w in up for w in _OK_WORDS) else ""


def _lead_time(view: dict) -> str:
    """리드 타임이 없으면 `?m` 같은 자리표시자를 찍지 않는다 — 값처럼 보여서 버그로
    읽힌다. 측정이 없었다는 사실을 그대로 쓴다."""
    v = view.get("lead_time_min")
    if v is None or v == "":
        return '<dd class="is-none">미측정</dd>'
    return f'<dd>{_e(v)}<span class="unit">분</span></dd>'


def _table(cls: str, head: str, rows: str, empty: str) -> str:
    """표는 반드시 `overflow-x:auto` 컨테이너 안에 둔다. 안 그러면 안 끊기는
    instance_id 하나가 본문 전체를 옆으로 민다(375px에서 문서 폭 657px)."""
    if not rows:
        return f'<p class="empty">{empty}</p>'
    return (f'<div class="table-wrap"><table class="{cls}"><thead><tr>{head}</tr></thead>'
            f"<tbody>{rows}</tbody></table></div>")


def render(view: dict) -> str:
    instances = list(view.get("instances", []))
    loops = list(view.get("loops", []))
    decisions = list(view.get("decisions", []))

    inst_rows = "".join(
        f'<tr><td class="c-id">{_e(i["instance_id"])}</td>'
        f'<td class="c-role">{_e(i["role"])}</td>'
        f'<td class="c-model">{_e(i["model"])}</td>'
        f'<td class="c-effort">{_e(i["effort"])}</td></tr>'
        for i in instances)
    loop_rows = "".join(
        f'<tr><td class="c-num">{_e(l["iteration"])}</td>'
        f'<td class="verdict {_tone(l["verdict"], "verdict-ok", "verdict-no")}">'
        f"{_e(l['verdict'])}</td></tr>"
        for l in loops)
    decision_items = "".join(f"<li><span>{_e(d)}</span></li>" for d in decisions)
    decision_html = (f'<ol class="decisions">{decision_items}</ol>' if decision_items
                     else '<p class="empty">기록된 결정이 없습니다.</p>')

    status = view["status"]
    return f"""<!doctype html>
<html lang="ko" data-template-version="{TEMPLATE_VERSION}">
<head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>{_e(view['task_id'])} Report</title>
<style>{_CSS}</style></head>
<body>
<main class="page">
<header class="masthead">
  <p class="eyebrow">dev-crew · 워크플로 실행 리포트</p>
  <h1>{_e(view['task_id'])}</h1>
  <p><span class="status {_tone(status, 'status-ok', 'status-no')}">\
{_e(status)}</span></p>
  <dl class="stats">
    <div class="stat"><dt>리드 타임</dt>{_lead_time(view)}</div>
    <div class="stat"><dt>인스턴스</dt><dd>{len(instances)}</dd></div>
    <div class="stat"><dt>리뷰 루프</dt><dd>{len(loops)}</dd></div>
    <div class="stat"><dt>결정</dt><dd>{len(decisions)}</dd></div>
  </dl>
</header>
<section class="panel"><h2 class="panel-title">Agent Instances
  <span class="panel-count">{len(instances)}</span></h2>
{_table("t-inst", "<th>ID</th><th>Role</th><th>Model</th><th>Effort</th>", inst_rows,
        "실행된 에이전트 인스턴스가 없습니다.")}
</section>
<section class="panel"><h2 class="panel-title">Review Loops
  <span class="panel-count">{len(loops)}</span></h2>
{_table("t-loop", "<th>#</th><th>Verdict</th>", loop_rows,
        "리뷰 루프가 돌지 않았습니다.")}
</section>
<section class="panel"><h2 class="panel-title">Decisions
  <span class="panel-count">{len(decisions)}</span></h2>
{decision_html}
</section>
</main>
</body></html>"""
