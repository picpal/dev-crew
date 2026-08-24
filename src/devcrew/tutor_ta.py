"""TUTOR_TA — 채점이 끝난 회차의 후속 질문에 답한다 (#19).

학습자는 해설을 이미 읽었고, 그걸로도 이해가 안 가는 것을 묻는다. 그래서 이 Agent는
저장된 해설에 갇히지 않고 **repo를 직접 읽는다** — 해설에 없는 맥락(호출자, 반대 경로,
그 결정이 없었다면 무엇이 깨지는가)이 대개 막힌 지점을 푼다.

**진행 중인 회차에는 붙지 않는다.** 채점 전에 질문을 받으면 "3번 보기 B가 왜 틀려?"가
형식상 질문인 채로 정답을 흘린다. 채점 후로 제한하면 그 경로가 구조적으로 사라진다.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from .quiz import Evidence, Question, verify_evidence
from .schema import Role
from .untrusted import NOTE, fence

ANSWER_LIMIT = 2000       # Slack 본문 상한보다 넉넉히 아래. 프롬프트에도 같은 값을 적었다
LETTERS = "ABCDEFGH"
DROPPED_NOTE = "\n\n_근거 {n}건은 대조에 실패해 제외했습니다_"
TRUNCATED_NOTE = "\n\n_… 답변이 길어 잘렸습니다_"


def _one(q: Question, idx: int, choice: int | None) -> str:
    ev = "\n".join(f"    - {e.path}:{e.start_line}-{e.end_line} — {e.quote}"
                   for e in q.evidence)
    # 범위 밖 선택은 미응답으로 처리한다 — 보기 개수를 벗어나거나 음수면 실제 선택이
    # 뭐든 렌더링할 수 없다. 범위로 조정하면 선택지 거짓말이 되므로 선택 불명만 표시한다.
    valid_choice = choice is not None and 0 <= choice < len(q.options)
    picked = "미응답" if not valid_choice else LETTERS[choice]
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


# 세션 **하나의 전체 예산**이다 — 첫 turn(repo 읽기)과 답변 turn을 합쳐 이 시간을
# 넘기면 실패로 접는다. 출제(600s)보다 짧게 잡는다: 대화는 리듬이 중요하고, 여기서
# 오래 매달리면 스레드 lock을 쥔 채 다음 질문까지 막는다.
TURN_TIMEOUT = 300.0

INTRO = ("아래는 학습자가 방금 푼 회차다. " + NOTE + "\n{context}\n"
         "이제 이 학습자의 후속 질문에 답한다. 회차의 해설을 되풀이하지 말고, "
         "필요하면 repo를 직접 읽어 막힌 지점을 풀어라.\n")
ASK = ("학습자의 질문이다. " + NOTE + "\n{question}\n"
       "스키마대로 답을 제출해라.\n")


class TutorTAError(Exception):
    """답변을 만들지 못했다. 사유는 메시지에 있고, 그대로 스레드에 표시된다."""


@dataclass
class Answer:
    session_id: str
    text: str
    citations: list[Evidence]
    dropped: int = 0
    provider: object | None = None


async def _open(orch, cfg, *, exec_id: str, repo_path: str, context: str, first: str):
    """세션을 열고 첫 질문까지 한 코루틴에서 끝낸다 — 상한이 전 구간을 덮게 한다."""
    tier = cfg.role_defaults[Role.TUTOR_TA].tier
    inst = await orch.spawn(Role.TUTOR_TA, tier, execution_id=exec_id,
                            node_id="followup", task_scope="회차 후속 질문 답변",
                            worktree=repo_path)
    sid = await orch.start_worker(inst, INTRO.format(context=context),
                                  conversational=True)
    out = await orch.adapters[inst.provider].send(sid, first)
    return sid, inst.provider, out


async def ask(orch, cfg, *, exec_id: str, repo_path: str, question: str,
              session_id: str | None, context: str | None,
              provider=None) -> Answer:
    """질문 하나에 답한다. 세션이 없으면 열고, 있으면 그 세션의 다음 turn으로 보낸다.

    `context`는 세션을 새로 열 때만 쓴다 — 이미 열린 세션은 회차를 이미 알고 있다.

    상한은 `spawn` 이후 **전 구간**을 감싼다. 실제 작업(repo 읽기)은 `start_worker` 안의
    첫 turn에서 일어나므로 `send`에만 걸면 정작 매달리는 쪽이 무방비다 (lessons C14).
    """
    fenced = ASK.format(question=fence("question", question))

    async def _turn():
        if session_id:
            return session_id, provider, await orch.adapters[provider].send(session_id, fenced)
        return await _open(orch, cfg, exec_id=exec_id, repo_path=repo_path,
                           context=context or "", first=fenced)

    try:
        sid, prov, out = await asyncio.wait_for(_turn(), timeout=TURN_TIMEOUT)
    except asyncio.TimeoutError as e:
        raise TutorTAError("TimeoutError") from e
    except Exception as e:
        raise TutorTAError(f"{type(e).__name__}: {e}") from e

    raw = out.structured if isinstance(out.structured, dict) else {}
    body = (raw.get("answer") or "").strip()
    if not body:
        raise TutorTAError("빈 답변")
    text, kept, dropped = clean_answer(body, parse_citations(raw.get("citations")),
                                       repo_path)
    return Answer(session_id=sid, text=text, citations=kept, dropped=dropped,
                  provider=prov)
