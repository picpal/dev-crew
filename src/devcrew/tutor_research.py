"""TUTOR_RESEARCH — 주제를 조사해 자료를 남기고 학습 리포트를 쓴다 (§10.8).

`@tutor <주제 문장>`(repo 접두 없음)의 1단계다. repo 모드(§10.7)와 갈리는 지점은
**근거가 어디서 오는가** 하나다 — 코드가 아니라 이 Agent가 웹에서 찾아 저장한 파일이다.

자료는 repo가 아니라 **회차 전용 디렉토리**에 남는다. 일회성 질문이 대부분이라
repo에 쌓으면 최신화되지 않는 스냅샷만 늘고 다음 회차는 그걸 안 쓴다. 매 요청 새로
조사하면 낡을 일이 없고, 회차가 죽을 때 디렉토리째 회수하면 된다.

출제는 여기서 하지 않는다. 학습자가 리포트를 읽은 뒤 버튼을 눌렀을 때 `tutor.issue_quiz`가
이 디렉토리를 `repo_path`로 받아 §10.7 파이프라인을 그대로 돈다 — 읽지 않은 자료로
시험을 보게 하지 않기 위해서다.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from pathlib import Path

from .quiz import AUTHOR_FAILED_EVENT, Evidence, verify_evidence
from .schema import Role
from .tutor_ta import HARD_LIMIT, TRUNCATED_NOTE, parse_citations
from .untrusted import NOTE, fence

# 세션 **하나의 전체 예산**이다 — 조사 turn과 제출 turn을 합쳐 이 시간을 넘기면
# 실패로 접는다. 상한을 마지막 `send`에만 걸면 정작 매달리는 쪽(웹을 오가는 첫 turn)이
# 무방비가 된다 (lessons C14). 출제(600s)보다 길게 잡는다 — 네트워크가 끼어 있다.
TURN_TIMEOUT = 900.0

# 저장된 자료 중 몇 개까지 화면에 나열할지. 자료 목록은 "무엇을 근거로 읽었나"를
# 보여주는 것이지 파일 탐색기가 아니다.
MAX_LISTED_FILES = 12

# 자료 파일 머리의 출처 표시. role prompt가 이 형식을 지시하고, **하네스가 파일을 열어
# 확인한다** — 지시만 하고 검사하지 않으면 그건 강제가 아니라 부탁이다.
SOURCE_LINE_RE = re.compile(r"^\s*>\s*출처\s*[:：]\s*(https?://\S+)", re.M)

INTRO = (
    "학습자가 배우고 싶어 하는 주제다. " + NOTE + "\n{topic}\n"
    "이 주제를 조사해라. `WebSearch`로 찾고 `WebFetch`로 읽은 뒤, 근거가 될 내용을\n"
    "**현재 디렉토리에 `.md` 파일로** 저장해라(`Write`). 그 파일들이 이후 모든 근거의\n"
    "원본이고, 하네스가 파일을 열어 인용을 대조한다 — 저장하지 않은 것은 근거가 되지\n"
    "못한다. 저장을 마쳤으면 그 자료에 근거해 학습 리포트를 써라.\n")
NUDGE = "이제 리포트와 근거를 스키마대로 제출해라."


class ResearchError(Exception):
    """조사에 실패했다. 사유는 메시지에 있고 그대로 스레드에 표시된다."""


@dataclass
class ResearchResult:
    topic: str
    corpus_dir: str
    report: str
    citations: list[Evidence]
    sources: list[dict] = field(default_factory=list)
    files: list[str] = field(default_factory=list)   # 하네스가 디렉토리에서 직접 센 것
    dropped: int = 0
    diagram: str | None = None
    status: str = ""
    summary: str = ""
    notes: list[str] = field(default_factory=list)


def corpus_files(corpus_dir: str | Path) -> list[str]:
    """저장된 자료 목록 — **하네스가 디렉토리를 읽어 센다.**

    모델에게 "무슨 파일을 만들었냐"고 묻지 않는다. 그건 이미 하네스가 아는 값이고,
    모델 주장과 실제가 갈리면 화면이 거짓말을 한다 (lessons C2·C5).

    확장자를 가리지 않는다. `.md`만 세면 출처 검사도 `.md`만 보게 되고, 모델이
    `.txt`로 저장한 자료가 목록에도 검사에도 안 잡힌 채 **출제 근거로는 쓰인다** —
    인용 대조는 디렉토리 안의 아무 파일이나 열기 때문이다. 숨김 파일만 뺀다.
    """
    root = Path(corpus_dir)
    if not root.is_dir():
        return []
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*")
                  if p.is_file() and not any(part.startswith(".")
                                             for part in p.relative_to(root).parts))


def verify_sources(corpus_dir: str | Path) -> tuple[list[str], list[str]]:
    """자료 파일마다 출처 URL이 실제로 박혀 있는지 연다 → `(있는 파일, 없는 파일)`.

    LLM 판단이 아니라 결정적 검사다 — 인용 대조(`verify_evidence`)와 같은 성격이고,
    같은 이유로 존재한다. 출처 없는 자료는 모델이 지어낸 것과 화면에서 구분되지 않고,
    이 자료는 그대로 출제 근거가 되므로 그 거짓이 문항으로 굳는다.
    """
    root = Path(corpus_dir)
    with_src: list[str] = []
    without: list[str] = []
    for name in corpus_files(root):
        try:
            text = (root / name).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            without.append(name)
            continue
        (with_src if SOURCE_LINE_RE.search(text) else without).append(name)
    return with_src, without


def parse_sources(raw) -> list[dict]:
    """모델이 낸 sources → `{url, title}`. 형태가 틀린 항목은 버린다.

    URL은 화면에 링크로 나가므로 스킴을 검사한다 — `javascript:` 같은 값이 리포트에
    앵커로 실리면 CSP가 스크립트를 막아도 클릭 유도가 남는다.
    """
    out: list[dict] = []
    for r in raw or []:
        if not isinstance(r, dict):
            continue
        url, title = r.get("url"), r.get("title")
        if not isinstance(url, str) or not isinstance(title, str):
            continue
        if not url.startswith(("http://", "https://")):
            continue
        out.append({"url": url, "title": title.strip() or url})
    return out


def clean_report(text: str, citations: list[Evidence],
                 corpus_dir: str) -> tuple[str, list[Evidence], int]:
    """인용을 자료 파일에 대조하고 본문을 다듬는다 → `(본문, 통과 인용, 버린 개수)`.

    출제와 달리 **대조 실패가 리포트 폐기 사유가 아니다** — 틀린 인용만 떼고 본문은
    낸다(`tutor_ta.clean_answer`와 같은 처분). 다만 뗐다는 사실은 호출자가 화면에
    적는다. 조용히 지우면 고친 것이 새 거짓말이 된다 (lessons C12).

    **대조가 보장하는 것은 "이 문장이 그 파일에 있다"까지다.** 파일 내용이 참인지는
    하네스가 알 수 없다 — 근거가 코드였던 §10.7과 다른 점이고, 그래서 출처를 함께
    보여준다 (§10.8 D4).
    """
    kept, dropped = verify_evidence(citations, corpus_dir)
    body = (text or "").strip()
    if len(body) > HARD_LIMIT:
        body = body[:HARD_LIMIT] + TRUNCATED_NOTE
    return body, kept, len(dropped)


async def _reclaim(orch, inst, sid: str | None) -> None:
    """세션 하나를 반납한다 — 워커 프로세스(`archive`)와 registry 행(`finish`) 둘 다.
    best-effort다. 정리하다 난 실패가 원래 예외를 가려서는 안 된다."""
    if sid is not None:
        try:
            await orch.adapters[inst.provider].archive(sid)
        except Exception:
            pass
    try:
        orch.registry.finish(inst.instance_id)
    except Exception:
        pass


async def research(orch, cfg, *, exec_id: str, topic: str, corpus_dir: str | Path,
                   progress=None) -> ResearchResult:
    """주제 하나를 조사해 자료를 남기고 리포트를 만든다.

    `progress(stage)`는 단계마다 불린다(async). 조사는 웹을 오가느라 길다 — 그동안
    아무 말도 못 하면 사용자에게는 무반응과 구분되지 않는다 (2026-08-24 출제에서
    같은 문제를 겪었다). 진행 표시가 실패해도 조사는 계속한다.
    """
    async def _tick(stage: str) -> None:
        if progress is None:
            return
        try:
            await progress(stage)
        except Exception:
            pass

    root = Path(corpus_dir)
    root.mkdir(parents=True, exist_ok=True)
    tier = cfg.role_defaults[Role.TUTOR_RESEARCH].tier
    inst = None
    # `sid`를 **밖에** 둔다. 안쪽 코루틴의 지역변수로 두면 상한에 걸려 취소됐을 때
    # 세션을 가리키는 것이 프로그램 어디에도 남지 않아 반납할 손잡이가 사라진다
    # (2026-08-24, `tutor._ask`·`tutor_ta._open`과 같은 결함).
    sid: str | None = None
    try:
        inst = await orch.spawn(Role.TUTOR_RESEARCH, tier, execution_id=exec_id,
                                node_id="research", task_scope="주제 조사",
                                worktree=str(root))

        async def _both_turns():
            # **첫 turn이 실제 작업이다.** 검색·수집·파일 저장이 전부 여기서 일어나고,
            # 이어지는 nudge는 스키마대로 제출만 시킨다. 상한이 두 turn을 함께 덮는다.
            nonlocal sid
            sid = await orch.start_worker(inst, INTRO.format(
                topic=fence("learning-topic", topic)))
            return await orch.adapters[inst.provider].send(sid, NUDGE)

        await _tick("자료 조사 중…")
        out = await asyncio.wait_for(_both_turns(), timeout=TURN_TIMEOUT)
    except Exception as e:
        reason = f"{type(e).__name__}: {e}"
        # **사유를 trace에 남긴다.** 화면에는 "조사에 실패했습니다"만 남으므로,
        # 여기 안 적으면 원인을 나중에 볼 방법이 없다 (lessons C10).
        try:
            orch.trace.append(AUTHOR_FAILED_EVENT, task_id=exec_id, execution_id=exec_id,
                              payload={"role": Role.TUTOR_RESEARCH.value,
                                       "node": "research", "reason": reason})
        except Exception:
            pass
        raise ResearchError(reason) from e
    finally:
        if inst is not None:
            await _reclaim(orch, inst, sid)

    raw = out.structured if isinstance(out.structured, dict) else None
    if not raw:
        raise ResearchError("조사 결과가 스키마대로 오지 않았습니다")

    await _tick("근거 대조 중…")
    body, kept, dropped = clean_report(raw.get("report"),
                                       parse_citations(raw.get("citations")), str(root))
    files = corpus_files(root)
    sources = parse_sources(raw.get("sources"))
    cited, no_source = verify_sources(root)

    # ── 신뢰도 관문 ─────────────────────────────────────────────────────────
    # **통과 조건을 쓴다.** "무엇을 막을까"로 쓰면 응답이 비었을 때 전부 통과한다
    # (lessons C8). 아래 넷이 모두 갖춰져야 리포트가 학습자에게 간다.
    #
    # 프롬프트에도 같은 규칙이 적혀 있지만 그건 부탁이다 — 지키지 않아도 화면에는
    # 성실한 리포트로 보이고, 그 자료가 그대로 출제 근거가 되어 거짓이 문항으로 굳는다.
    missing = []
    if not files:
        missing.append("자료 파일이 하나도 저장되지 않았습니다")
    elif no_source:
        # 일부만 없어도 거절한다. 출처 없는 파일도 출제 근거로 쓰이므로, 남겨 두면
        # 그 문항의 출처를 학습자가 추적할 방법이 없다.
        missing.append("출처 표시가 없는 자료 파일: " + ", ".join(no_source[:5]))
    if not sources:
        missing.append("출처 목록이 비어 있습니다")
    if not kept:
        missing.append("자료에 대조되는 인용이 하나도 없습니다")
    if not body:
        missing.append("리포트 본문이 비어 있습니다")
    if missing:
        reason = "근거를 갖추지 못해 리포트를 내지 않았습니다 — " + " / ".join(missing)
        try:
            orch.trace.append(AUTHOR_FAILED_EVENT, task_id=exec_id, execution_id=exec_id,
                              payload={"role": Role.TUTOR_RESEARCH.value,
                                       "node": "research-gate", "reason": reason,
                                       "files": files, "no_source": no_source,
                                       "sources": len(sources), "citations": len(kept)})
        except Exception:
            pass
        raise ResearchError(reason)

    notes: list[str] = []
    if dropped:
        notes.append(f"근거 {dropped}건은 대조에 실패해 제외했습니다")
    return ResearchResult(
        topic=topic, corpus_dir=str(root), report=body, citations=kept,
        sources=sources, files=files, dropped=dropped,
        diagram=raw.get("diagram") if isinstance(raw.get("diagram"), str) else None,
        status=str(raw.get("status") or ""), summary=str(raw.get("summary") or ""),
        notes=notes)
