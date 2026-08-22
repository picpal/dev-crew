"""p10 — 학습 회차 리포트 렌더 확인 (LLM 없음).

이 저장소 자신을 대상으로, 실제 파일에 대고 인용 대조를 통과한 문항만으로
리포트를 그린다. 지어낸 인용 하나를 섞어 게이트가 실제로 버리는지 함께 본다.

    PYTHONPATH=src python poc/p10_quiz_report.py [출력경로]
"""
import sys
from pathlib import Path

from devcrew.quiz import grade, parse_questions, verify_citations
from devcrew.report.quiz_report import render_quiz_report

ROOT = Path(__file__).resolve().parents[1]

DRAFT = [
    {"area": "인용 대조", "type": "CORRECT",
     "stem": "출제된 문항의 근거를 검증하는 방식으로 옳은 것은?",
     "options": ["검증자 LLM이 근거의 실재까지 판단한다",
                 "하네스가 파일을 열어 인용이 실재하는지 대조한다",
                 "근거는 기록만 하고 검증하지 않는다",
                 "출제 모델이 스스로 재확인한다"],
     "answer_index": 1,
     "evidence": [{"path": "src/devcrew/quiz.py", "start_line": 1, "end_line": 12,
                   "quote": "하네스가 그 근거를 실제 파일에 대조해 지어낸 것을 버린다"}],
     "explanation": "1차 방어는 LLM 판단이 아니라 결정적 검사다. 검증자는 그다음 관문으로, "
                    "'인용이 정답을 뒷받침하는가'라는 다른 질문을 맡는다.",
     "diagram": {"type": "flow",
                 "nodes": [{"id": "a", "label": "출제 (Claude)"},
                           {"id": "b", "label": "인용 대조 (하네스)"},
                           {"id": "c", "label": "교차 검증 (Codex)"},
                           {"id": "d", "label": "선별 10문항"}],
                 "edges": [{"from": "a", "to": "b", "label": "12문항"},
                           {"from": "b", "to": "c", "label": "실재하는 근거만"},
                           {"from": "c", "to": "d", "label": "PASS만"}]},
     "source_key": None},
    {"area": "오답 노트", "type": "INCORRECT",
     "stem": "오답 노트 설계로 틀린 것은?",
     "options": ["trace의 append-only 이벤트로 둔다",
                 "열림·해소는 rowid 순서로 판정한다",
                 "별도 SQLite 스토어를 새로 만든다",
                 "사용자·repo 단위로 키를 나눈다"],
     "answer_index": 2,
     "evidence": [{"path": "src/devcrew/quiz.py", "start_line": 200, "end_line": 206,
                   "quote": "오답 노트의 execution_id — 사용자·repo 단위"}],
     "explanation": "새 스토어를 만들지 않는다. trace가 이미 append-only 이벤트 로그이고 "
                    "execution_id로 조회되므로 그대로 쓴다.",
     "diagram": {"type": "bar", "unit": "개",
                 "items": [{"label": "새 스토어", "value": 0},
                           {"label": "재사용한 스토어", "value": 1},
                           {"label": "이벤트 종류", "value": 4}]},
     "source_key": None},
    {"area": "리포트", "type": "CORRECT",
     "stem": "리포트에서 클릭 펼침을 구현한 방식으로 옳은 것은?",
     "options": ["인라인 <script>로 토글한다",
                 "CDN의 UI 라이브러리를 쓴다",
                 "<details>/<summary>로 JS 없이 구현한다",
                 "서버가 매번 다시 렌더한다"],
     "answer_index": 2,
     "evidence": [{"path": "src/devcrew/report/quiz_report.py", "start_line": 1,
                   "end_line": 12, "quote": "클릭 펼침은 `<details>/<summary>`로 한다"}],
     "explanation": "스크립트가 필요 없고, CSP의 script-src 미포함(escape 구멍의 backstop)을 "
                    "그대로 둘 수 있다.",
     "diagram": None, "source_key": None},
    {"area": "지어낸 문항", "type": "CORRECT",
     "stem": "이 문항은 없는 인용을 달고 있어 폐기돼야 한다",
     "options": ["A", "B", "C", "D"], "answer_index": 0,
     "evidence": [{"path": "src/devcrew/quiz.py", "start_line": 3, "end_line": 4,
                   "quote": "이 문장은 그 파일에 존재하지 않는다"}],
     "explanation": "폐기 대상", "diagram": None, "source_key": None},
]


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/quiz-report.html")
    parsed = parse_questions(DRAFT)
    ok, dropped = verify_citations(parsed, ROOT)
    print(f"파싱 {len(parsed)} → 인용 대조 통과 {len(ok)} / 폐기 {len(dropped)}")
    for q, reason in dropped:
        print(f"  ✗ {q.stem[:40]} — {reason}")
    card = grade(ok, {0: 1, 1: 0, 2: 2})       # 1·3번 정답, 2번 오답
    out.write_text(render_quiz_report(card, repo="dev-crew", added=1, cleared=2))
    print(f"채점 {card.correct}/{card.total} · 리포트 → {out}")


if __name__ == "__main__":
    main()
