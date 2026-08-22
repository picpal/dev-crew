"""p11 — 도식 갤러리. `render_diagram`이 낼 수 있는 **모든 모양**을 한 장에 편다.

리포트 한 장을 눈으로 보고 "괜찮다"고 판단하면 끝값이 통째로 안 보인다. 실제로
2026-08-21에 이 갤러리에서 세 건이 나왔다 — 음수 값(막대는 0인데 라벨은 -30%),
`%`인데 260(비율이 거짓이 됨), 간선 없는 자리의 화살표(없는 흐름을 주장).

도식 렌더러를 고쳤으면 이걸 다시 그려서 브라우저로 봐라. 브라우저 창 폭을 못 바꾸는
환경이라면 iframe을 뷰포트로 쓰는 하네스를 함께 쓴다 (docs 참조).

    PYTHONPATH=src python poc/p11_diagram_gallery.py [출력디렉터리]
"""

import html as _h
import pathlib
import sys
from devcrew.report.charts import CHART_CSS, render_diagram, bar_chart
from devcrew.report.quiz_report import TOKENS

CASES = [
 ("bar · 100% 기준 (영역별 정답률)", None, lambda: bar_chart(
     [("역할과 권한 경계", 33.3), ("모델 라우팅과 에스컬레이션 정책의 경계 조건", 66.7),
      ("세션", 100.0), ("리뷰 루프와 병렬화", 0.0)], unit="%", full=100.0)),
 ("bar · 최댓값 기준 (단위 %가 아님)", {"type": "bar", "unit": "건",
     "items": [{"label": "explore", "value": 3}, {"label": "develop", "value": 17},
               {"label": "review", "value": 8}]}, None),
 ("bar · 단위 없음 · 소수", {"type": "bar",
     "items": [{"label": "평균 루프 횟수", "value": 2.4},
               {"label": "최대", "value": 5}]}, None),
 ("bar · 항목 1개", {"type": "bar", "unit": "%", "items": [{"label": "커버리지", "value": 72}]}, None),
 ("bar · 음수와 상한 초과 (모델이 흘린 값)", {"type": "bar", "unit": "%",
     "items": [{"label": "정상", "value": 40}, {"label": "음수", "value": -30},
               {"label": "초과", "value": 260}]}, None),
 ("bar · 값이 전부 0", {"type": "bar", "unit": "%",
     "items": [{"label": "a", "value": 0}, {"label": "b", "value": 0}]}, None),
 ("bar · 라벨이 아주 김", {"type": "bar", "unit": "%", "items": [
     {"label": "재시작 시 전제조건만 검증하고 실제 resume은 다음 dispatch로 미루는 노드의 비율",
      "value": 88}]}, None),
 ("flow · 노드 1개", {"type": "flow", "nodes": [{"id": "a", "label": "ToolCallEvent"}]}, None),
 ("flow · 간선 없음 (노드만)", {"type": "flow", "nodes": [
     {"id": "a", "label": "explore"}, {"id": "b", "label": "develop"},
     {"id": "c", "label": "review"}]}, None),
 ("flow · 노드 8개", {"type": "flow",
     "nodes": [{"id": str(i), "label": f"{i}단계 — 노드 라벨"} for i in range(8)],
     "edges": [{"from": str(i), "to": str(i + 1), "label": f"전이 {i}"} for i in range(7)]}, None),
 ("flow · 비순차 간선만", {"type": "flow",
     "nodes": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}, {"id": "c", "label": "C"}],
     "edges": [{"from": "c", "to": "a", "label": "되돌림"},
               {"from": "a", "to": "c", "label": "건너뜀"}]}, None),
 ("flow · 라벨이 아주 긴 노드", {"type": "flow", "nodes": [
     {"id": "a", "label": "harness가 소유한 can_use_tool 콜백이 allowlist 밖 호출을 거부하고 "
                          "ToolCallEvent로 trace에 기록한다"},
     {"id": "b", "label": "b"}],
     "edges": [{"from": "a", "to": "b", "label": "이 간선 라벨도 제법 길게 적어 본다 — 두 줄까지"}]}, None),
 ("표 폴백 · 미지원 타입 · 열 6개", {"type": "sankey", "items": [
     {"단계": "explore", "provider": "claude-code", "tier": "high", "토큰": 18400,
      "소요(초)": 42, "worktree": ".worktrees/wt-a"},
     {"단계": "develop", "provider": "claude-code", "tier": "high", "토큰": 92100,
      "소요(초)": 311, "worktree": ".worktrees/wt-a"},
     {"단계": "review", "provider": "codex", "tier": "medium", "토큰": 31200,
      "소요(초)": 88, "worktree": ".worktrees/wt-a"}]}, None),
 ("표 폴백 · 값에 태그가 섞임", {"type": "unknown", "items": [
     {"label": "<script>alert(1)</script>", "value": "<b>x</b>"}]}, None),
 ("빈 결과 (아무것도 그리지 않아야 함)", {"type": "bar", "items": []}, None),
 ("빈 결과 · 노드 없는 flow", {"type": "flow", "edges": [{"from": "a", "to": "b"}]}, None),
]

blocks = []
for title, spec, fn in CASES:
    body = fn() if fn else render_diagram(spec)
    empty = ' <span class="empty">(빈 출력)</span>' if not body.strip() else ""
    blocks.append(f'<section class="case"><h2>{_h.escape(title)}{empty}</h2>{body}</section>')

CSS = TOKENS + """
*{box-sizing:border-box}
body{margin:0;background:var(--plane);color:var(--ink-1);line-height:1.6;
  font-family:system-ui,-apple-system,"Apple SD Gothic Neo",sans-serif}
.page{max-width:56rem;margin:0 auto;padding:2rem 1rem 5rem}
h1{font-size:1.4rem;margin:0 0 1.5rem}
.case{background:var(--surface);border:1px solid var(--line);border-radius:14px;
  padding:1rem 1.2rem 1.2rem;margin:0 0 1rem}
.case h2{margin:0 0 .2rem;font-size:.85rem;font-weight:650;color:var(--ink-2)}
.empty{color:var(--ink-3);font-weight:400}
""" + CHART_CSS

out_dir = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
out = out_dir / "diagram-gallery.html"
out.write_text(f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>도식 갤러리</title><style>{CSS}</style></head><body>
<main class="page"><h1>render_diagram — 모든 모양</h1>{''.join(blocks)}</main></body></html>""")
print(out, len(out.read_text()), "bytes")
