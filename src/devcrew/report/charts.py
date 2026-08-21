"""도식 스펙(JSON) → HTML 조각 (#19). 모델은 데이터만 내고 그림은 하네스가 그린다.

모델이 SVG/HTML을 직접 쓰게 하지 않는 이유는 셋이다.
1. 리포트는 텍스트 슬롯만 제공한다(#6) — raw HTML 삽입 경로를 만들면 그 원칙이 깨진다.
2. SVG 안의 수치는 evidence와 대조할 수 없다. JSON 값이어야 검증이 가능하다.
3. 깨진 SVG는 그대로 게시되지만, 스펙은 표로 폴백할 수 있다.

**그림은 SVG가 아니라 HTML+CSS로 그린다** (2026-08-21). SVG `<text>`는 줄바꿈이
없어서 "모델 라우팅과 에스컬레이션" 같은 한글 라벨이 고정폭 라벨 칸을 넘어 그림 밖으로
새어 나갔다 — 사용자가 본 "글자가 영역에서 벗어난다"가 이것이다. HTML 텍스트는 접히고,
막대는 `%` 폭이라 컨테이너를 따라 줄어든다. CSP(`style-src 'unsafe-inline'`)는 그대로다.

지원 타입은 `bar`와 `flow` 둘이다. 그 밖은 표로 흘린다 — 그림을 못 그렸다고
해설을 잃지 않는다.
"""
from __future__ import annotations

import html as _html

# 차트 CSS. 리포트가 `<style>`에 함께 넣는다 (외부 요청 0, 파일 하나).
# 색은 리포트가 정의한 토큰(--series-1 …)을 쓴다 — 차트가 자기 색을 들고 다니면
# 라이트/다크 전환이 두 군데로 갈린다.
CHART_CSS = """
.chart{display:grid;gap:.55rem;margin:.9rem 0}
.chart-row{display:grid;grid-template-columns:minmax(0,10.5rem) 1fr auto;
           align-items:center;gap:.7rem}
.chart-label{min-width:0;overflow-wrap:anywhere;color:var(--ink-2);font-size:.82rem;
             line-height:1.35}
.chart-track{position:relative;height:10px;border-radius:999px;background:var(--grid);
             min-width:0}
.chart-fill{height:100%;border-radius:0 4px 4px 0;background:var(--series-1);
            min-width:2px}
.chart-value{font-size:.82rem;color:var(--ink-1);font-variant-numeric:tabular-nums;
             white-space:nowrap}
@media (max-width:520px){
  .chart{gap:1rem}
  .chart-row{grid-template-columns:1fr auto;gap:.3rem .7rem;
             grid-template-areas:"label value" "track track"}
  .chart-label{grid-area:label}.chart-value{grid-area:value}.chart-track{grid-area:track}
}
.flow{display:grid;gap:0;margin:.9rem 0;justify-items:stretch}
.flow-node{border:1px solid var(--line);border-radius:10px;background:var(--surface-2);
           padding:.55rem .8rem;font-size:.85rem;line-height:1.45;overflow-wrap:anywhere}
.flow-link{display:flex;align-items:center;gap:.45rem;padding:.15rem 0 .15rem .9rem;
           color:var(--ink-3);font-size:.75rem;line-height:1.3}
.flow-arrow{color:var(--line-strong)}
.flow-extra-title{margin:.75rem 0 .1rem;color:var(--ink-3);font-size:.68rem;
                  letter-spacing:.08em;text-transform:uppercase}
.flow-extra{margin:.1rem 0 0;padding:0;list-style:none;color:var(--ink-3);
            font-size:.75rem;line-height:1.6}
.table-wrap{overflow-x:auto;margin:.9rem 0}
.table-wrap table{border-collapse:collapse;min-width:100%;font-size:.8rem}
.table-wrap th,.table-wrap td{border-bottom:1px solid var(--line);padding:.4rem .6rem;
                              text-align:left;white-space:nowrap}
.table-wrap th{color:var(--ink-3);font-weight:600}
"""


def _e(v) -> str:
    return _html.escape(str(v), quote=True)


def _fmt(v: float) -> str:
    """정수는 소수 꼬리 없이 — `0.0개`는 읽기 나쁘다."""
    return str(int(v)) if float(v).is_integer() else str(v)


def _num(v) -> float | None:
    """모델이 문자열 수치("많음")를 흘려도 그래프가 깨지지 않게 한다."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v)


def bar_chart(items: list[tuple[str, float]], *, unit: str = "",
              full: float | None = None) -> str:
    """가로 막대. 요약의 영역별 정답률과 `bar` 도식이 함께 쓴다.

    막대 하나에 색 하나다. 값이 클수록 진하게 칠하면 길이가 이미 말한 것을 색으로
    한 번 더 말하는 셈이고(값 램프를 명목 범주에), 남은 유일한 채널을 태운다.

    `full`은 100%가 무엇인지 아는 경우(정답률 등)에 준다. 없으면 최댓값 기준이라
    "모두 40%"인 회차가 "하나는 꽉 참"으로 보인다 — 비교의 기준이 데이터마다
    달라지는 것이 막대 그래프의 흔한 거짓말이다."""
    if not items:
        return ""
    top = full if full and full > 0 else (max((v for _, v in items), default=0) or 1)
    rows = []
    for label, value in items:
        pct = max(0.0, min(100.0, value * 100.0 / top))
        rows.append(
            f'<div class="chart-row">'
            f'<div class="chart-label">{_e(label)}</div>'
            f'<div class="chart-track"><div class="chart-fill" style="width:{pct:.1f}%">'
            f'</div></div>'
            f'<div class="chart-value">{_e(_fmt(value))}{_e(unit)}</div></div>')
    return '<div class="chart">' + "".join(rows) + "</div>"


def _bar_spec(spec: dict) -> str:
    items = [(it.get("label", ""), v)
             for it in (spec.get("items") or []) if isinstance(it, dict)
             and (v := _num(it.get("value"))) is not None]
    unit = str(spec.get("unit") or "")
    return bar_chart(items, unit=unit, full=100.0 if unit == "%" else None)


def _flow_spec(spec: dict) -> str:
    """세로 흐름도. 노드를 순서대로 쌓고 이어지는 두 노드 사이에 화살표를 둔다.

    상자 크기를 고정하지 않는다 — 라벨이 길면 상자가 자란다. 예전 SVG 판은 150px
    상자에 12px 텍스트를 중앙 정렬해서, 긴 라벨이 상자 밖으로 밀려 나갔다.
    순서대로 이어지지 않는 간선은 지어내 그리지 않고 아래에 목록으로 남긴다."""
    nodes = [n for n in (spec.get("nodes") or []) if isinstance(n, dict) and n.get("id")]
    if not nodes:
        return ""
    order = {str(n["id"]): i for i, n in enumerate(nodes)}
    label_of = {str(n["id"]): str(n.get("label", n["id"])) for n in nodes}
    edges = [e for e in (spec.get("edges") or []) if isinstance(e, dict)]
    chain, extra = {}, []
    for e in edges:
        a, b = str(e.get("from")), str(e.get("to"))
        i, j = order.get(a), order.get(b)
        if i is not None and j == i + 1 and i not in chain:
            chain[i] = str(e.get("label") or "")
        elif i is not None and j is not None:   # 없는 노드를 가리키면 조용히 버린다
            extra.append((a, b, str(e.get("label") or "")))
    parts = []
    for i, n in enumerate(nodes):
        parts.append(f'<div class="flow-node">{_e(label_of[str(n["id"])])}</div>')
        if i in chain:
            text = f'<span>{_e(chain[i])}</span>' if chain[i] else ""
            parts.append(f'<div class="flow-link"><span class="flow-arrow">↓</span>'
                         f'{text}</div>')
        elif i + 1 < len(nodes):
            parts.append('<div class="flow-link"><span class="flow-arrow">↓</span></div>')
    out = '<div class="flow">' + "".join(parts) + "</div>"
    if extra:
        items = "".join(
            f'<li>{_e(label_of.get(a, a))} → {_e(label_of.get(b, b))}'
            + (f' · {_e(lbl)}' if lbl else "") + "</li>" for a, b, lbl in extra)
        out += ('<p class="flow-extra-title">그 밖의 연결</p>'
                f'<ul class="flow-extra">{items}</ul>')
    return out


def _table_fallback(spec: dict) -> str:
    """지원하지 않는 타입 — 값만이라도 읽게 표로 흘린다.

    가로 스크롤 컨테이너에 넣는다. 열이 많은 표를 그냥 두면 좁은 화면에서 본문 전체가
    옆으로 밀린다."""
    items = [it for it in (spec.get("items") or []) if isinstance(it, dict)]
    if not items:
        return ""
    keys = list(dict.fromkeys(k for it in items for k in it))
    head = "".join(f"<th>{_e(k)}</th>" for k in keys)
    rows = "".join("<tr>" + "".join(f"<td>{_e(it.get(k, ''))}</td>" for k in keys) + "</tr>"
                   for it in items)
    return ('<div class="table-wrap"><table><thead><tr>' + head
            + "</tr></thead><tbody>" + rows + "</tbody></table></div>")


def render_diagram(spec: dict | None) -> str:
    """도식 스펙 → HTML 조각. 못 그리면 표, 그마저 불가하면 빈 문자열."""
    if not isinstance(spec, dict):
        return ""
    try:
        kind = str(spec.get("type") or "")
        if kind == "bar":
            return _bar_spec(spec)
        if kind == "flow":
            return _flow_spec(spec)
        return _table_fallback(spec)
    except Exception:
        return ""
