"""TUTOR_TA — 채점이 끝난 회차의 후속 질문에 답한다 (#19).

학습자는 해설을 이미 읽었고, 그걸로도 이해가 안 가는 것을 묻는다. 그래서 이 Agent는
저장된 해설에 갇히지 않고 **repo를 직접 읽는다** — 해설에 없는 맥락(호출자, 반대 경로,
그 결정이 없었다면 무엇이 깨지는가)이 대개 막힌 지점을 푼다.

**진행 중인 회차에는 붙지 않는다.** 채점 전에 질문을 받으면 "3번 보기 B가 왜 틀려?"가
형식상 질문인 채로 정답을 흘린다. 채점 후로 제한하면 그 경로가 구조적으로 사라진다.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .quiz import Evidence, Question, verify_evidence

ANSWER_LIMIT = 2000       # Slack 본문 상한보다 넉넉히 아래. 프롬프트에도 같은 값을 적었다
LETTERS = "ABCDEFGH"
DROPPED_NOTE = "\n\n_근거 {n}건은 대조에 실패해 제외했습니다_"
TRUNCATED_NOTE = "\n\n_… 답변이 길어 잘렸습니다_"


def _one(q: Question, idx: int, choice: int | None) -> str:
    ev = "\n".join(f"    - {e.path}:{e.start_line}-{e.end_line} — {e.quote}"
                   for e in q.evidence)
    picked = "미응답" if choice is None else LETTERS[choice]
    mark = "오답" if choice != q.answer_index else "정답"
    opts = "\n".join(f"    {LETTERS[i]}) {o}" for i, o in enumerate(q.options))
    return (f"[{idx + 1}] ({mark}) 영역: {q.area}\n"
            f"  지문: {q.stem}\n{opts}\n"
            f"  정답: {LETTERS[q.answer_index]}   학습자 선택: {picked}\n"
            f"  해설: {q.explanation}\n  근거:\n{ev}")


def round_context(questions: list[Question], answers: dict[int, int], *,
                  repo_name: str) -> str:
    """세션 시작 시 1회 주입하는 회차 전체.

    **틀린 문항을 먼저 놓는다** — 질문은 대개 거기서 나오고, 앞에 있어야 모델이 먼저
    본다. 채점이 끝난 뒤이므로 정답·해설을 감출 이유가 없다.
    """
    order = sorted(range(len(questions)),
                   key=lambda i: (answers.get(i) == questions[i].answer_index, i))
    body = "\n\n".join(_one(questions[i], i, answers.get(i)) for i in order)
    return (f"대상 repo: {repo_name}\n"
            f"아래는 방금 채점이 끝난 회차다. 틀린 문항이 앞에 있다.\n\n{body}")


def parse_citations(raw) -> list[Evidence]:
    """모델이 낸 citations → Evidence. 형태가 틀린 항목은 버린다.

    모델 출력은 신뢰하지 않는다. 여기서 거른 것은 대조 이전 단계의 쓰레기이고,
    실재 여부는 `clean_answer`가 파일을 열어 따로 확인한다.
    """
    out: list[Evidence] = []
    for r in raw or []:
        if not isinstance(r, dict):
            continue
        p, s, e, q = r.get("path"), r.get("start_line"), r.get("end_line"), r.get("quote")
        if not isinstance(p, str) or not isinstance(q, str):
            continue
        if not isinstance(s, int) or not isinstance(e, int) or isinstance(s, bool):
            continue
        out.append(Evidence(path=p, start_line=s, end_line=e, quote=q))
    return out


def clean_answer(text: str, citations: list[Evidence],
                 repo_path: str) -> tuple[str, list[Evidence], int]:
    """인용을 대조하고 본문을 다듬는다 → `(본문, 통과 인용, 버린 개수)`.

    출제와 달리 **대조 실패가 답변 폐기 사유가 아니다.** 틀린 인용만 떼고 본문은 낸다.
    다만 뗐다는 사실은 화면에 적는다 — 조용히 지우면 고친 것이 새 거짓말이 된다
    (lessons C12).
    """
    kept, dropped = verify_evidence(citations, repo_path)
    body = (text or "").strip()
    if len(body) > ANSWER_LIMIT:
        body = body[:ANSWER_LIMIT] + TRUNCATED_NOTE
    if dropped:
        body += DROPPED_NOTE.format(n=len(dropped))
    return body, kept, len(dropped)
