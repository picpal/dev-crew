"""도식 스펙(JSON) → 인라인 SVG (#19). 모델은 데이터만 내고 그림은 하네스가 그린다.

모델이 SVG를 직접 쓰게 하지 않는 이유는 셋이다.
1. 리포트는 텍스트 슬롯만 제공한다(#6) — raw HTML 삽입 경로를 만들면 그 원칙이 깨진다.
2. SVG 안의 수치는 evidence와 대조할 수 없다. JSON 값이어야 검증이 가능하다.
3. 깨진 SVG는 그대로 게시되지만, 스펙은 표로 폴백할 수 있다.

지원 타입은 `bar`와 `flow` 둘이다. 그 밖은 표로 흘린다 — 그림을 못 그렸다고
해설을 잃지 않는다.
"""
from __future__ import annotations

import html as _html

BAR_W, BAR_H, BAR_GAP, LABEL_W = 320, 22, 8, 120
NODE_W, NODE_H, NODE_GAP = 150, 44, 34


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


def bar_chart(items: list[tuple[str, float]], *, unit: str = "") -> str:
    """가로 막대. 요약의 영역별 정답률과 `bar` 도식이 함께 쓴다."""
    if not items:
        return ""
    top = max((v for _, v in items), default=0) or 1
    h = len(items) * (BAR_H + BAR_GAP) + BAR_GAP
    width = LABEL_W + BAR_W + 60
    rows = []
    for i, (label, value) in enumerate(items):
        y = BAR_GAP + i * (BAR_H + BAR_GAP)
        w = max(1, int(BAR_W * value / top))
        rows.append(
            f'<text x="{LABEL_W - 6}" y="{y + 15}" text-anchor="end" font-size="12">{_e(label)}</text>'
            f'<rect x="{LABEL_W}" y="{y}" width="{w}" height="{BAR_H}" fill="#333"/>'
            f'<text x="{LABEL_W + w + 6}" y="{y + 15}" font-size="12">{_e(_fmt(value))}{_e(unit)}</text>')
    return (f'<svg viewBox="0 0 {width} {h}" width="100%" role="img" '
            f'style="max-width:{width}px">' + "".join(rows) + "</svg>")


def _bar_spec(spec: dict) -> str:
    items = [(it.get("label", ""), v)
             for it in (spec.get("items") or []) if isinstance(it, dict)
             and (v := _num(it.get("value"))) is not None]
    return bar_chart(items, unit=str(spec.get("unit") or ""))


def _flow_spec(spec: dict) -> str:
    """세로 흐름도. 노드 상자를 위에서 아래로 놓고 화살표로 잇는다."""
    nodes = [n for n in (spec.get("nodes") or []) if isinstance(n, dict) and n.get("id")]
    if not nodes:
        return ""
    pos = {str(n["id"]): i for i, n in enumerate(nodes)}
    h = len(nodes) * (NODE_H + NODE_GAP)
    width = NODE_W + 200
    parts = ['<defs><marker id="a" markerWidth="8" markerHeight="8" refX="7" refY="3" '
             'orient="auto"><path d="M0,0 L0,6 L7,3 z" fill="#333"/></marker></defs>']
    for i, n in enumerate(nodes):
        y = i * (NODE_H + NODE_GAP)
        parts.append(
            f'<rect x="20" y="{y}" width="{NODE_W}" height="{NODE_H}" fill="none" stroke="#333"/>'
            f'<text x="{20 + NODE_W // 2}" y="{y + NODE_H // 2 + 4}" text-anchor="middle" '
            f'font-size="12">{_e(n.get("label", n["id"]))}</text>')
    for edge in (spec.get("edges") or []):
        if not isinstance(edge, dict):
            continue
        a, b = pos.get(str(edge.get("from"))), pos.get(str(edge.get("to")))
        if a is None or b is None:          # 모델이 없는 노드를 가리켜도 그림은 남는다
            continue
        y1 = a * (NODE_H + NODE_GAP) + NODE_H
        y2 = b * (NODE_H + NODE_GAP)
        parts.append(f'<line x1="{20 + NODE_W // 2}" y1="{y1}" x2="{20 + NODE_W // 2}" '
                     f'y2="{y2}" stroke="#333" marker-end="url(#a)"/>')
        if edge.get("label"):
            parts.append(f'<text x="{28 + NODE_W}" y="{(y1 + y2) // 2 + 4}" '
                         f'font-size="11">{_e(edge["label"])}</text>')
    return (f'<svg viewBox="0 0 {width} {h}" width="100%" role="img" '
            f'style="max-width:{width}px">' + "".join(parts) + "</svg>")


def _table_fallback(spec: dict) -> str:
    """지원하지 않는 타입 — 값만이라도 읽게 표로 흘린다."""
    items = [it for it in (spec.get("items") or []) if isinstance(it, dict)]
    if not items:
        return ""
    keys = list(dict.fromkeys(k for it in items for k in it))
    head = "".join(f"<th>{_e(k)}</th>" for k in keys)
    rows = "".join("<tr>" + "".join(f"<td>{_e(it.get(k, ''))}</td>" for k in keys) + "</tr>"
                   for it in items)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table>"


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
