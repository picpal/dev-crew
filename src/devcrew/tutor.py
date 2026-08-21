"""출제 파이프라인 (#19) — 출제 → 인용 대조 → Codex 교차 검증 → 선별.

세 관문이 순서대로 문항을 깎는다.

1. **인용 대조** (`quiz.verify_citations`) — 하네스가 파일을 열어 근거의 실재를 확인한다.
   LLM 판단이 아니라 결정적 검사다.
2. **교차 검증** (`TUTOR_VERIFIER`) — 출제와 **다른 provider**의 별도 인스턴스가
   "근거가 정답을 뒷받침하는가"를 판정한다. 출제 세션의 논증은 주지 않는다 —
   출제자의 근거를 보면 그대로 수긍하기 때문이다.
3. **선별** — 오답 노트 유래를 먼저, 그다음 영역 다양성.

부족하면 보충 1회. 그래도 미달이면 **채운 만큼만 낸다** — 숫자를 맞추려다 지어내는 것이
이 Agent의 최악 실패다.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

from .quiz import Question, parse_questions, verify_citations
from .schema import Role
from .untrusted import NOTE, fence

DRAFT_COUNT = 12          # 통과 후 10문항이 남도록 여유를 두고 출제한다
QUIZ_COUNT = 10
MAX_CARRIED = 3           # 한 회차에서 오답 노트가 가져갈 수 있는 자리
TURN_TIMEOUT = 600.0      # 출제는 repo를 읽는 agentic turn이라 인터뷰보다 길다


@dataclass
class IssueResult:
    questions: list[Question]
    shortfall: bool = False
    notes: list[str] = field(default_factory=list)   # 사용자에게 알릴 사유


AUTHOR_INTRO = (
    "이 repository에 대해 학습용 4지선다 문항 {n}개를 출제해라.\n"
    "프로세스와 핵심 내용을 묻는다. 문항마다 evidence(파일 경로·줄 범위·원문 인용)를 단다.\n"
    "하네스가 그 파일을 열어 인용이 실재하는지 대조하고, 다른 모델이 근거와 정답의 정합성을\n"
    "다시 판정한다 — 지어낸 근거는 통과하지 못한다. 근거를 못 찾은 문항은 내지 마라.\n")
REISSUE_BLOCK = (
    "\n아래는 이 사용자가 지난 회차에 틀린 문항의 근거다. " + NOTE + "\n{misses}\n"
    "이 중 최대 {k}개를 **같은 근거로 다시 출제**하되 지문을 바꿔라 — 답을 외운 것과\n"
    "이해한 것을 구분해야 한다. 재출제 문항에는 그 항목의 `q_key`를 `source_key`에 넣어라.\n"
    "나머지 문항의 `source_key`는 null이다.\n")
TOPUP_INTRO = (
    "앞서 낸 문항 중 {k}개가 탈락했다. 사유는 아래와 같다. " + NOTE + "\n{reasons}\n"
    "같은 실수를 반복하지 말고 {n}개를 새로 출제해라. 근거를 못 찾으면 그 문항은 내지 마라.\n")
VERIFY_INTRO = (
    "다른 모델이 출제한 학습 문항을 교차 검증한다. 너는 출제 과정을 보지 않았다 —\n"
    "repo를 직접 읽어 스스로 판정해라. " + NOTE + "\n{questions}\n"
    "받은 문항 전부에 대해 index별로 PASS/REJECT를 내라. 확신이 없으면 REJECT다.\n")


def _verifier_view(questions: list[Question]) -> str:
    """검증자에게 보이는 문항 — 출제 세션의 맥락 없이 판정에 필요한 것만."""
    return json.dumps([{
        "index": i, "area": q.area, "type": q.type, "stem": q.stem,
        "options": q.options, "answer": q.answer, "explanation": q.explanation,
        "diagram": q.diagram,
        "evidence": [{"path": e.path, "start_line": e.start_line,
                      "end_line": e.end_line, "quote": e.quote} for e in q.evidence],
    } for i, q in enumerate(questions)], ensure_ascii=False, indent=1)


def select_ten(pool: list[Question], *, count: int = QUIZ_COUNT) -> list[Question]:
    """통과 문항에서 회차를 고른다. 오답 유래 → 영역 다양성 → 출제 순서.

    영역 다양성을 두는 이유는, 통과 문항이 한 파일에 몰리면 회차 전체가 그 한 구석만
    묻게 되기 때문이다 — 리포트의 영역별 카드도 의미를 잃는다."""
    # key 중복을 먼저 없앤다. 같은 key가 한 회차에 둘 들어오면 하나는 miss, 하나는
    # clear로 기록돼 최종 오답 상태가 문항 순서에 좌우된다 (Codex 리뷰 2026-08-21).
    uniq, seen_keys = [], set()
    for q in pool:
        if q.key not in seen_keys:
            seen_keys.add(q.key)
            uniq.append(q)
    picked = [q for q in uniq if q.carried][:MAX_CARRIED]
    seen_areas = {q.area for q in picked}
    # 상한을 넘은 오답 유래 문항은 나머지 후보에서도 뺀다 — 안 그러면 상한이 무의미하다
    rest = [q for q in uniq if q not in picked and not q.carried]
    for q in rest:                              # 1순위: 아직 안 나온 영역
        if len(picked) >= count:
            break
        if q.area not in seen_areas:
            picked.append(q)
            seen_areas.add(q.area)
    for q in rest:                              # 2순위: 나머지를 출제 순서대로
        if len(picked) >= count:
            break
        if q not in picked:
            picked.append(q)
    return picked[:count]


async def _ask(orch, cfg, role: Role, *, exec_id: str, node_id: str, scope: str,
               worktree: str | None, intro: str, nudge: str) -> dict | None:
    """role 인스턴스 하나를 띄워 구조화 출력 하나를 받는다. 실패는 None."""
    tier = cfg.role_defaults[role].tier
    inst = await orch.spawn(role, tier, execution_id=exec_id, node_id=node_id,
                            task_scope=scope, worktree=worktree)
    try:
        sid = await orch.start_worker(inst, intro)
        out = await asyncio.wait_for(
            orch.adapters[inst.provider].send(sid, nudge), timeout=TURN_TIMEOUT)
    except Exception:
        return None
    finally:
        try:
            orch.registry.finish(inst.instance_id)      # 1회용 인스턴스 반납
        except Exception:
            pass
    return out.structured if isinstance(out.structured, dict) else None


async def _author(orch, cfg, *, exec_id, repo_path, intro, allowed_keys):
    raw = await _ask(orch, cfg, Role.TUTOR, exec_id=exec_id, node_id="author",
                     scope="학습 문항 출제", worktree=repo_path, intro=intro,
                     nudge="이제 문항을 스키마대로 제출해라.")
    if not raw:
        return []
    return parse_questions(raw.get("questions"), allowed_keys=allowed_keys)


async def _verify(orch, cfg, *, exec_id, repo_path,
                  questions: list[Question]) -> tuple[list[Question], list[str]]:
    """검증자 판정 적용. **검증이 실패하면 아무것도 통과시키지 않는다** —
    검증 없는 문항이 사람에게 가는 것이 이 파이프라인의 실패 모드다."""
    if not questions:
        return [], []
    intro = VERIFY_INTRO.format(questions=fence("questions", _verifier_view(questions)))
    raw = await _ask(orch, cfg, Role.TUTOR_VERIFIER, exec_id=exec_id, node_id="verify",
                     scope="문항 교차 검증", worktree=repo_path, intro=intro,
                     nudge="이제 판정을 스키마대로 제출해라.")
    verdicts = (raw or {}).get("verdicts")
    if not raw or raw.get("status") != "PASS" or not isinstance(verdicts, list):
        return [], ["교차 검증에 실패해 이번 출제를 통과시키지 않았습니다"]
    # 판정이 온전하지 않으면 전부 폐기한다. 누락된 인덱스를 묵시적 PASS로 읽으면
    # 검증자가 조용히 답을 줄이는 것만으로 관문을 우회할 수 있다 (Codex 리뷰 2026-08-21).
    seen: dict[int, dict] = {}
    for v in verdicts:
        if not isinstance(v, dict) or v.get("verdict") not in ("PASS", "REJECT"):
            continue
        try:
            i = int(v["index"])
        except (KeyError, TypeError, ValueError):
            continue
        if i in seen or not 0 <= i < len(questions):
            return [], ["교차 검증 판정이 중복·범위 밖입니다 — 통과시키지 않았습니다"]
        seen[i] = v
    if len(seen) != len(questions):
        return [], [f"교차 검증이 {len(questions)}문항 중 {len(seen)}건만 판정했습니다 "
                    f"— 통과시키지 않았습니다"]
    rejected = {i: str(v.get("reason") or "사유 없음")
                for i, v in seen.items() if v["verdict"] == "REJECT"}
    passed = [q for i, q in enumerate(questions) if i not in rejected]
    return passed, [f"{questions[i].stem} — {r}" for i, r in rejected.items()]


async def issue_quiz(orch, cfg, *, repo_name: str | None, repo_path: str | None,
                     exec_id: str, misses: list[dict],
                     count: int = QUIZ_COUNT) -> IssueResult:
    """한 회차를 출제한다. 반환 문항이 `count`보다 적으면 `shortfall`이 선다."""
    notes: list[str] = []
    allowed = {m["q_key"]: m.get("evidence") or []
               for m in misses if m.get("q_key")}
    intro = AUTHOR_INTRO.format(n=DRAFT_COUNT)
    if misses:
        intro += REISSUE_BLOCK.format(
            misses=fence("prior-misses", json.dumps(misses[:MAX_CARRIED],
                                                    ensure_ascii=False, indent=1)),
            k=MAX_CARRIED)

    drafted = await _author(orch, cfg, exec_id=exec_id, repo_path=repo_path,
                            intro=intro, allowed_keys=allowed)
    cited, dropped = verify_citations(drafted, repo_path or ".")
    if dropped:
        notes.append(f"인용 대조에서 {len(dropped)}문항 폐기 "
                     f"({', '.join(sorted({r for _, r in dropped}))})")
    verified, rejects = await _verify(orch, cfg, exec_id=exec_id,
                                      repo_path=repo_path, questions=cited)
    if rejects:
        notes.append(f"교차 검증에서 {len(rejects)}문항 반려")

    if len(verified) < count:
        # 보충 1회 — 반려 사유를 함께 넣어 같은 실수를 반복하지 않게 한다
        need = count - len(verified)
        top_intro = AUTHOR_INTRO.format(n=need + 2) + TOPUP_INTRO.format(
            k=len(dropped) + len(rejects), n=need + 2,
            reasons=fence("reject-reasons", "\n".join(
                rejects + [f"{q.stem} — {r}" for q, r in dropped]) or "사유 없음"))
        extra = await _author(orch, cfg, exec_id=exec_id, repo_path=repo_path,
                              intro=top_intro, allowed_keys=allowed)
        extra_cited, _ = verify_citations(extra, repo_path or ".")
        extra_ok, _ = await _verify(orch, cfg, exec_id=exec_id,
                                    repo_path=repo_path, questions=extra_cited)
        seen = {q.key for q in verified}
        verified += [q for q in extra_ok if q.key not in seen]

    picked = select_ten(verified, count=count)
    if len(picked) < count:
        notes.append(f"근거를 찾지 못해 {len(picked)}문항만 출제했습니다")
    return IssueResult(questions=picked, shortfall=len(picked) < count, notes=notes)
