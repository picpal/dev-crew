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

# 이 길이를 넘으면 **자르지 않고 HTML 리포트로 흘린다** — 예전에는 여기서 무조건
# 잘라 "답변이 길어 잘렸습니다"만 남았고 학습자는 나머지를 볼 방법이 없었다.
# Slack 한 메시지 상한(3000)보다 넉넉히 아래에 둔다.
SLACK_LIMIT = 2000
# 리포트도 무한하지 않다. 여기서만 자른다 — 모델이 폭주해도 페이지가 터지지 않게 하는
# 마지막 방어선이고, 정상 답변은 여기 근처에도 오지 않는다.
HARD_LIMIT = 20000
HEAD_LIMIT = 400          # 리포트로 흘렸을 때 스레드에 남기는 머리말(첫 문단) 상한
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

    **길다고 자르지 않는다.** SLACK_LIMIT을 넘는 본문은 호출자가 HTML 리포트로 흘리고
    스레드에는 머리말+링크만 남긴다(`slack_head`). 여기서 자르는 자리는 HARD_LIMIT
    하나뿐이고, 그건 폭주 방어선이지 표시 정책이 아니다.
    """
    kept, dropped = verify_evidence(citations, repo_path)
    body = (text or "").strip()
    if len(body) > HARD_LIMIT:
        body = body[:HARD_LIMIT] + TRUNCATED_NOTE
    if dropped:
        body += DROPPED_NOTE.format(n=len(dropped))
    return body, kept, len(dropped)


def slack_head(body: str, *, dropped: int = 0, limit: int = HEAD_LIMIT) -> str:
    """리포트로 흘린 답변의 스레드 머리말 — **첫 문단**.

    프롬프트가 "핵심 한 문장을 먼저 쓰라"고 지시하므로 첫 문단이 곧 요약이다. 링크만
    남기면 무엇에 대한 답인지 스레드만 봐서는 알 수 없고, 2000자를 그대로 남기면
    리포트로 뺀 의미가 없다.

    **버린 인용 공개는 머리말에도 붙인다.** 본문 끝에만 있으면 리포트를 열지 않은
    학습자에게는 공개가 사라진다 (lessons C12).
    """
    head = (body or "").strip().split("\n\n")[0].strip()
    if len(head) > limit:
        head = head[:limit].rstrip() + "…"
    if dropped:
        head += DROPPED_NOTE.format(n=dropped)
    return head


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
    # 모델이 스스로 붙인 판정과 한 줄 요약. 화면에는 안 나가지만 **trace에는 남긴다** —
    # 스키마 거절 루프에 걸린 모델은 검증만 통과할 최소 payload를 내는데(`answer: "test"`,
    # `summary: "test"`), 본문만 기록해서는 성실한 답인지 때운 것인지 구분할 수 없다 (C15).
    status: str = ""
    summary: str = ""
    # 실행 흐름을 보여줄 대상. 있으면 호출자가 TUTOR_CODE로 리포트를 만든다.
    # **작게 유지한다** — 여기에 코드나 스텝을 담으면 스키마에 큰 필드가 둘이 되고,
    # 순서로는 하나밖에 못 지켜 앞엣것이 뒤엣것을 삼킨다 (lessons C15).
    code_focus: dict | None = None
    # 무엇을 그릴지에 대한 **산문 설명**. 그리기는 TUTOR_VIS가 한다.
    # 여기에 SVG를 담지 않는 이유도 C15다 — 큰 필드는 하나로 족하다.
    diagram: str | None = None
    provider: object | None = None
    # 반납 손잡이. `archive`는 session_id로 하지만 `registry.finish`는 instance_id로만
    # 할 수 있어서, 호출자가 회차를 닫을 때 둘 다 필요하다.
    instance_id: str | None = None


async def _reclaim(orch, inst, sid: str) -> None:
    """세션 하나를 반납한다 — 워커 프로세스(`archive`)와 registry 행(`finish`) 둘 다.

    best-effort다. 정리하다 난 실패가 원래 예외를 가려서는 안 된다.
    """
    try:
        await orch.adapters[inst.provider].archive(sid)
    except Exception:
        pass
    try:
        orch.registry.finish(inst.instance_id)
    except Exception:
        pass


async def _open(orch, cfg, *, exec_id: str, repo_path: str, context: str, first: str):
    """세션을 열고 첫 질문까지 한 코루틴에서 끝낸다 — 상한이 전 구간을 덮게 한다.

    두 turn 중 무거운 쪽은 두 번째다 — `start_worker`(위 INTRO)는 회차 맥락만 주는
    가벼운 turn이고, 실제 질문(`first`)이 들어가는 `send`에서 모델이 repo를 읽는다.
    `send`가 이 함수 안에서 실패하거나 상한에 걸려 취소되면 `sid`는 아직 호출자에게
    반환되지 않은 상태다 — 프로그램 어디에도 이 세션을 가리키는 손잡이가 남지 않고,
    워커 프로세스는 archive 없이 계속 돈다(2026-08-24 재발; 그날 아침 어댑터에 넣은
    가드는 `start_session`의 첫 turn만 지키고 여기(호출자 쪽 두 번째 turn)는 비어
    있었다). 그래서 여기가 이 sid를 회수할 수 있는 마지막 자리다. 정리는 best-effort로
    한다 — 정리 실패가 원래 예외를 가려서는 안 된다.
    """
    tier = cfg.role_defaults[Role.TUTOR_TA].tier
    inst = await orch.spawn(Role.TUTOR_TA, tier, execution_id=exec_id,
                            node_id="followup", task_scope="회차 후속 질문 답변",
                            worktree=repo_path)
    # `conversational=True`를 쓰지 않는다. 그 스위치의 유일한 효과는 **output_schema
    # 주입을 생략하는 것**이고(orchestrator.start_worker), 스키마 없이 뜬 세션은
    # `structured_output`을 절대 내지 않는다 — 그러면 아래 `ask`의 `out.structured`가
    # 항상 None이라 모든 질문이 "빈 답변"으로 실패한다 (2026-08-24 최종 리뷰 C1:
    # 기능이 프로덕션에서 한 번도 동작하지 않았다). 세션이 turn을 넘어 유지되는 것은
    # 이 스위치와 무관하다 — BRAIN 인터뷰가 스키마를 끄는 이유는 "자연어 대화가 매 turn
    # JSON이 되면 안 되기 때문"인데, TUTOR_TA는 매 turn이 스키마 제출이라 반대다.
    sid = await orch.start_worker(inst, INTRO.format(context=context))
    try:
        out = await orch.adapters[inst.provider].send(sid, first)
    except BaseException:
        await _reclaim(orch, inst, sid)
        raise
    return sid, inst, out


async def ask(orch, cfg, *, exec_id: str, repo_path: str, question: str,
              session_id: str | None, context: str | None,
              provider=None, instance_id: str | None = None) -> Answer:
    """질문 하나에 답한다. 세션이 없으면 열고, 있으면 그 세션의 다음 turn으로 보낸다.

    `context`는 세션을 새로 열 때만 쓴다 — 이미 열린 세션은 회차를 이미 알고 있다.

    상한은 `spawn` 이후 **전 구간**을 감싼다. `_open`의 두 turn 중 실제 작업(repo
    읽기)은 두 번째(`send`)에서 일어나므로, 상한을 그 turn 하나에만 걸면 정작
    매달리는 쪽이 무방비다 (lessons C14) — 그래서 `_open` 전체를 감싼다.

    `session_id`와 `provider`는 항상 짝이다 — 두 번째 질문부터는 이전 `Answer`의
    `provider`를 그대로 넘겨야 한다. 짝 없이 `session_id`만 오면 `orch.adapters[None]`이
    raw `KeyError`를 내고 그게 그대로 학습자 화면에 노출되므로 여기서 미리 막는다.
    `instance_id`도 같이 넘기면 그대로 돌려준다 — 재사용 경로는 새 instance를 만들지
    않으므로, 넘기지 않으면 호출자가 반납 손잡이를 잃는다.

    이 adapter 인스턴스가 `session_id`를 모르면(예: cross-process 재시작으로 세션이
    유실) — `context`가 있으면 새 세션으로 자연 복구한다. `context`가 없으면 복구할
    회차 맥락이 없다는 뜻이라, 죽은 session_id로 되돌아가 무한 재시도에 갇히지 않도록
    사유를 남기고 멈춘다.
    """
    if session_id and provider is None:
        raise TutorTAError(
            "session_id는 있는데 provider가 없다 — 두 번째 질문부터는 이전 "
            "Answer.provider를 그대로 넘겨야 한다")

    fenced = ASK.format(question=fence("question", question))

    async def _turn():
        # 세 번째 값이 `inst`인 이유: 재사용 경로(None)와 새로 연 경로를 이 자리에서
        # 구분해야 아래 회수 가드가 "이번에 내가 연 세션"만 반납할 수 있다.
        if session_id:
            try:
                return session_id, None, await orch.adapters[provider].send(
                    session_id, fenced)
            except KeyError as e:
                # 이 프로세스의 이 adapter 인스턴스가 session_id를 모른다는 신호다
                # (`send`가 내부 딕셔너리에서 session_id를 못 찾음) — cross-process
                # 재시작 등으로 흔히 생긴다. context가 있으면 그걸로 새 세션을 열어
                # 자연 복구하고, 없으면 죽은 session_id로 되돌아가지 않도록 멈춘다.
                if not context:
                    raise TutorTAError(
                        "이전 대화 세션을 더 이상 찾을 수 없고, 다시 열 회차 맥락도 "
                        "없습니다 — 새 질문으로 다시 시작해 주세요.") from e
                return await _open(orch, cfg, exec_id=exec_id, repo_path=repo_path,
                                   context=context, first=fenced)
        return await _open(orch, cfg, exec_id=exec_id, repo_path=repo_path,
                           context=context or "", first=fenced)

    try:
        sid, inst, out = await asyncio.wait_for(_turn(), timeout=TURN_TIMEOUT)
    except asyncio.TimeoutError as e:
        raise TutorTAError("TimeoutError") from e
    except TutorTAError:
        raise
    except Exception as e:
        raise TutorTAError(f"{type(e).__name__}: {e}") from e

    # **여기부터 반환까지도 회수 구간이다.** `_open`은 자기 `send`만 감싸지만, 이
    # 아래에서 raise하면(예: `빈 답변`) sid는 아직 이 함수의 지역 변수뿐이라 호출자는
    # 손잡이를 받지 못한다 — `_open`의 실패와 결과가 정확히 같다: 프로그램 어디에도
    # 그 워커를 가리키는 것이 남지 않는다 (2026-08-24 최종 리뷰 C2).
    # 재사용 경로(`inst is None`)는 반납하지 않는다 — 그 세션의 손잡이는 이미 호출자가
    # 들고 있어 고아가 아니고, 여기서 죽이면 다음 질문에 쓸 세션을 말없이 없애는 것이다.
    try:
        raw = out.structured if isinstance(out.structured, dict) else {}
        body = (raw.get("answer") or "").strip()
        if not body:
            raise TutorTAError("빈 답변")
        text, kept, dropped = clean_answer(body, parse_citations(raw.get("citations")),
                                           repo_path)
        status = str(raw.get("status") or "")
        summary = str(raw.get("summary") or "")
        focus = raw.get("code_focus")
        # 모델 출력은 신뢰하지 않는다 — 형태가 아니면 없는 것으로 본다. 경로의
        # 실재·repo 안 여부는 `tutor_code.resolve_source`가 파일을 열어 따로 막는다.
        if not isinstance(focus, dict) or not isinstance(focus.get("path"), str) \
                or not focus["path"].strip():
            focus = None
        spec = raw.get("diagram")
        spec = spec.strip() if isinstance(spec, str) and spec.strip() else None
    except BaseException:
        if inst is not None:
            await _reclaim(orch, inst, sid)
        raise
    return Answer(session_id=sid, text=text, citations=kept, dropped=dropped,
                  status=status, summary=summary, code_focus=focus, diagram=spec,
                  provider=inst.provider if inst is not None else provider,
                  instance_id=inst.instance_id if inst is not None else instance_id)
