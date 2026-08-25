"""학습 퀴즈의 순수 로직 (#19) — 문항 모델, 인용 대조, 채점, 오답 노트.

이 모듈에는 LLM도 Slack도 없다. 파이프라인은 `tutor.py`, Slack 흐름은 `slack_tutor.py`.

설계의 중심은 **근거 강제**다. 모델이 낸 문항은 근거(evidence)를 달고 오고, 하네스가
그 근거를 실제 파일에 대조해 지어낸 것을 버린다 — 판정을 LLM에 다시 묻지 않는다.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

class NoteUnavailable(Exception):
    """오답 노트를 읽지 못했다. **빈 노트와 구분해야 한다** — 조회 실패를 '오답 없음'으로
    바꾸면 기존 오답이 재출제되지도, 맞혀도 해소되지도 않고 사용자는 그 사실을 모른다."""


QUESTION_TYPES = ("CORRECT", "INCORRECT")
OPTION_COUNT = 4
_WS = re.compile(r"\s+")


@dataclass(frozen=True)
class Evidence:
    path: str
    start_line: int
    end_line: int
    quote: str


@dataclass
class Question:
    area: str
    type: str                      # CORRECT | INCORRECT
    stem: str
    options: list[str]
    answer_index: int
    evidence: list[Evidence]
    explanation: str
    diagram: dict | None = None
    key: str = ""
    carried: bool = False          # 오답 노트에서 재출제된 문항인가

    @property
    def answer(self) -> str:
        return self.options[self.answer_index]


def q_key(area: str, evidence: list[Evidence]) -> str:
    """문항 동일성 키 — 지문이 아니라 **어느 지점을 묻는가**로 정한다.

    같은 근거를 다른 각도에서 물어도 같은 문항으로 본다. 그래야 "틀린 문항을 나중에
    맞히면 오답 노트에서 뺀다"가 지문을 바꿔 재출제하는 방식과 함께 성립한다.
    끝 줄은 키에 넣지 않는다 — 모델이 같은 지점을 40–52로 인용했다가 38–55로 인용하는
    흔들림까지 다른 문항으로 갈라지면 오답이 영원히 안 지워진다."""
    parts = sorted(f"{e.path}:{e.start_line}" for e in evidence)
    return hashlib.sha256("|".join([area, *parts]).encode()).hexdigest()[:12]


def _evidence_of(raw) -> list[Evidence] | None:
    if not isinstance(raw, list):
        return None
    out = []
    for e in raw:
        if not isinstance(e, dict):
            return None
        try:
            out.append(Evidence(str(e["path"]), int(e["start_line"]),
                                int(e["end_line"]), str(e["quote"])))
        except (KeyError, TypeError, ValueError):
            return None
    return out


def _origin_slots(origin) -> dict[str, set[tuple[str, int]]]:
    """이월 후보 키 → 원래 오답이 가리키던 (경로, 시작 줄) 집합."""
    if isinstance(origin, dict):
        return {k: {(str(e.get("path")), int(e.get("start_line", 0)))
                    for e in (v or []) if isinstance(e, dict)}
                for k, v in origin.items()}
    return {k: set() for k in (origin or set())}


def parse_questions(raw: list[dict],
                    allowed_keys: dict[str, list[dict]] | None = None,
                    *, trusted_keys: set[str] | None = None) -> list[Question]:
    """모델 출력 → 문항. 형태가 어긋난 항목은 조용히 버린다.

    재출제 문항은 `source_key`로 **원래 키를 이월**한다. 키를 매번 재계산하면 모델이
    같은 지점을 40–52로 인용했다가 38–55로 인용하는 순간 오답 해소가 끊긴다.
    다만 이월은 **하네스가 발급한 키**(`allowed_keys`)일 때만 받는다 — 모델이 키를
    지어내도 남의 오답을 해소하지 못한다.

    근거가 **비어 있는** 문항은 여기서 버리지 않는다 — 인용 대조가 사유와 함께 버려야
    사용자에게 "왜 문항이 줄었는지" 설명할 수 있다."""
    slots = _origin_slots(allowed_keys)
    trusted = trusted_keys or set()
    out: list[Question] = []
    for r in (raw or []):
        if not isinstance(r, dict):
            continue
        options, ev = r.get("options"), _evidence_of(r.get("evidence"))
        if ev is None or not isinstance(options, list) or len(options) != OPTION_COUNT:
            continue
        if r.get("type") not in QUESTION_TYPES:
            continue
        try:
            idx = int(r["answer_index"])
            area, stem = str(r["area"]), str(r["stem"])
            explanation = str(r["explanation"])
        except (KeyError, TypeError, ValueError):
            continue
        if not 0 <= idx < OPTION_COUNT:
            continue
        diagram = r.get("diagram")
        # 이월은 "같은 지점을 다시 물었을 때"만 성립한다. 키만 보고 받으면 전혀 다른
        # 문항이 남의 오답을 해소해 노트가 의미를 잃는다 (Codex 리뷰 2026-08-21).
        src = r.get("source_key")
        carried = None
        if isinstance(src, str) and src in trusted:
            # 하네스가 저장한 회차의 복원 — 모델의 주장이 아니라 우리가 쓴 값이다.
            # 여기서 대조를 요구하면 재개할 때마다 이월이 끊긴다 (Codex 3차 리뷰).
            carried = src
        elif isinstance(src, str) and src in slots:
            want = slots[src]
            here = {(e.path, e.start_line) for e in ev}
            # 원래 근거를 **전부** 다시 인용했을 때만 승계한다. 일부만 겹쳐도 받으면
            # 근거 하나를 갈아끼워 남의 오답을 해소할 수 있고, 대조할 근거가 아예 없으면
            # 키만 남아 검증이 불가능하다 (Codex 재리뷰 2026-08-21).
            carried = src if (want and want <= here) else None
        out.append(Question(
            area=area, type=r["type"], stem=stem, options=[str(o) for o in options],
            answer_index=idx, evidence=ev, explanation=explanation,
            diagram=diagram if isinstance(diagram, dict) else None,
            key=carried or q_key(area, ev), carried=carried is not None))
    return out


def _norm(s: str) -> str:
    return _WS.sub(" ", s).strip()


def _check_evidence(ev: Evidence, root: Path) -> str | None:
    """인용 하나를 대조한다. 통과면 None, 아니면 폐기 사유."""
    if ev.path.startswith("/") or ev.path.startswith("~"):
        return "path-outside-repo"
    try:
        target = (root / ev.path).resolve()
        if not target.is_relative_to(root):    # `..` 로 저장소 밖을 가리킴
            return "path-outside-repo"
        if not target.is_file():
            return "path-missing"
    except (OSError, ValueError, RuntimeError):
        # 경로 하나가 이상해서 회차 전체가 죽으면 안 된다 (NUL·심볼릭 루프 등)
        return "path-invalid"
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return "path-unreadable"
    start, end = ev.start_line, ev.end_line
    # 범위를 보정해서 통과시키지 않는다. 보정하면 문항에 거짓 위치가 표시되고,
    # q_key는 보정 전 값으로 계산돼 오답 노트가 어긋난다 (Codex 리뷰 2026-08-21).
    if not (1 <= start <= end <= len(lines)):
        return "line-range-invalid"
    # 모델은 줄바꿈·들여쓰기를 흘린다. 공백 차이로 진짜 근거를 버리면 안 되므로
    # 양쪽을 한 줄로 접어 비교한다 — 인용의 존재만 보고 형식은 보지 않는다.
    window = _norm(" ".join(lines[start - 1:end]))
    quote = _norm(ev.quote)
    if not quote or quote not in window:
        return "quote-not-in-range"
    return None


def verify_evidence(evs: list[Evidence],
                    root: str | Path) -> tuple[list[Evidence], list[tuple[Evidence, str]]]:
    """근거를 파일에 대조해 실재하는 것만 남긴다 (LLM 없음, 결정적).

    반환: `(통과, [(버린 evidence, 사유)])`.

    **처분은 호출자가 정한다.** 출제는 하나라도 가짜면 문항을 버리지만(장식 근거가
    정답의 근거인 척할 수 있다), 후속 질문 답변은 틀린 인용만 떼고 본문은 낸다.
    검사 자체는 하나여야 하므로 여기로 꺼냈다.
    """
    root = Path(root).resolve()
    kept: list[Evidence] = []
    dropped: list[tuple[Evidence, str]] = []
    for e in evs:
        reason = _check_evidence(e, root)
        (dropped.append((e, reason)) if reason else kept.append(e))
    return kept, dropped


def verify_citations(questions: list[Question],
                     root: str | Path) -> tuple[list[Question], list[tuple[Question, str]]]:
    """근거를 실제 파일에 대조해 지어낸 문항을 버린다 (LLM 없음, 결정적).

    반환: `(통과 문항, [(버린 문항, 사유)])`. 사유는 사용자에게 "왜 문항이 줄었는지"
    설명하는 데 쓰이므로 버리기만 하지 않고 이유를 들고 나온다.

    근거가 여러 개면 **전부** 실재해야 한다 — 하나만 진짜고 나머지가 장식이면,
    그 장식이 정답의 근거인 척할 수 있다."""
    root = Path(root).resolve()
    passed: list[Question] = []
    dropped: list[tuple[Question, str]] = []
    for q in questions:
        if not q.evidence:
            dropped.append((q, "no-evidence"))
            continue
        _kept, drops = verify_evidence(q.evidence, root)
        (dropped.append((q, drops[0][1])) if drops else passed.append(q))
    return passed, dropped


# ── 채점 ────────────────────────────────────────────────────────────────────
@dataclass
class Result:
    question: Question
    choice: int | None            # None = 미응답
    correct: bool


@dataclass
class Scorecard:
    total: int
    correct: int
    by_area: dict[str, tuple[int, int]] = field(default_factory=dict)   # (맞음, 전체)
    results: list[Result] = field(default_factory=list)

    def area_pct(self) -> list[tuple[str, float]]:
        """영역별 정답률 — 리포트 요약 막대의 입력."""
        return [(a, round(ok * 100.0 / n, 1)) for a, (ok, n) in self.by_area.items()]


def grade(questions: list[Question], answers: dict[int, int]) -> Scorecard:
    """문항 + 선택 → 채점. 미응답은 오답으로 센다 (중도 이탈 회차도 채점된다)."""
    results, by_area = [], {}
    for i, q in enumerate(questions):
        choice = answers.get(i)
        ok = choice == q.answer_index
        results.append(Result(question=q, choice=choice, correct=ok))
        c, n = by_area.get(q.area, (0, 0))
        by_area[q.area] = (c + (1 if ok else 0), n + 1)
    return Scorecard(total=len(questions), correct=sum(r.correct for r in results),
                     by_area=by_area, results=results)


# ── 오답 노트 (trace 이벤트) ────────────────────────────────────────────────
GRADED_EVENT = "QuizGradedEvent"   # 회차 채점 결과 — 오답·해소를 한 이벤트에 담는다
ISSUED_EVENT = "QuizIssuedEvent"
ANSWER_EVENT = "QuizAnswerEvent"
QUESTION_EVENT = "QuizQuestionEvent"     # 후속 질문 (#19)
TA_ANSWER_EVENT = "QuizTAAnswerEvent"    # 그 답변
# GRADED_EVENT는 note_id(사용자·repo) 네임스페이스에 쌓여 여러 회차가 공유한다 — 그래서
# "어느 회차가" 채점됐는지 특정하지 못한다. 회차 자신의 execution_id(QUIZ-*)에 남기는
# 이 이벤트만이 "이 회차가 채점됐다"를 안전하게 뜻한다 (#19, 리뷰 2026-08-24).
ROUND_GRADED_EVENT = "QuizRoundGradedEvent"
# 출제·검증 세션이 실패한 **사유**. `notes`는 메모리라 회차가 끝나면 사라지고,
# 사용자에게는 "문항을 만들지 못했습니다"만 남는다 — 그러면 원인을 영영 못 본다
# (2026-08-24 15:52 출제자가 6분 반을 쓰고 0문항을 냈는데 이유가 어디에도 없었다).
AUTHOR_FAILED_EVENT = "QuizAuthorFailedEvent"
# 리포트 발행이 실패한 사유. 같은 이유로 남긴다 (2026-08-24 16:29 실제 실패).
REPORT_FAILED_EVENT = "QuizReportFailedEvent"


def note_id(user: str, repo: str) -> str:
    """오답 노트의 execution_id — 사용자·repo 단위. 취약 영역은 사람마다 다르다."""
    return f"TUTOR-{user}-{repo}"


def open_misses(trace, exec_id: str) -> list[dict]:
    """지금 열려 있는 오답 — 회차 이벤트를 id 순서로 접어 남는 것.

    trace는 append-only라 상태가 아니라 이력이다. 벽시계가 아니라 **id 순서**로 접는다
    (같은 초에 여러 이벤트가 들어오면 ts로는 순서가 갈리지 않는다)."""
    try:
        evs = trace.events(execution_id=exec_id)
    except Exception as e:
        raise NoteUnavailable(str(e)) from e
    state: dict[str, dict] = {}
    for e in sorted(evs, key=lambda e: e["id"]):
        if e["event_type"] != GRADED_EVENT:
            continue
        payload = e.get("payload") or {}
        for key in payload.get("cleared") or []:
            state.pop(key, None)
        for miss in payload.get("missed") or []:
            if miss.get("q_key"):
                state[miss["q_key"]] = miss
    return list(state.values())


def record_scorecard(trace, exec_id: str, card: Scorecard,
                     prior_keys: set[str]) -> tuple[int, int]:
    """채점 결과를 오답 노트에 반영한다. 반환: (새 오답 수, 해소 수).

    **회차 하나 = 이벤트 하나.** 문항별로 쪼개 쓰면 중간에 실패했을 때 부분 반영이
    남고, 재시도가 그 위에 겹쳐 상태가 갈린다. 한 번의 append는 한 번의 커밋이라
    전부 쓰이거나 전혀 안 쓰이며, 같은 내용을 다시 써도 접은 결과가 같다
    (Codex 3차 리뷰 2026-08-21).

    해소는 **이전에 열려 있던 문항을 맞혔을 때만** 센다. 처음 맞힌 문항까지 clear로
    남기면 이력이 의미를 잃는다."""
    missed = [{"q_key": r.question.key, "area": r.question.area,
               "stem": r.question.stem,
               "evidence": [vars(e) for e in r.question.evidence]}
              for r in card.results if not r.correct]
    cleared = [r.question.key for r in card.results
               if r.correct and r.question.key in prior_keys]
    trace.append(GRADED_EVENT, task_id=exec_id, execution_id=exec_id,
                 payload={"missed": missed, "cleared": cleared})
    return len(missed), len(cleared)


def to_raw(q: Question) -> dict:
    """문항 → 저장 가능한 dict. `source_key`에 키를 실어 복원 시 그대로 이월된다.

    회차는 trace에 기록돼야 세션이 죽어도 이어 풀 수 있다 (append-only가 진실)."""
    return {"area": q.area, "type": q.type, "stem": q.stem, "options": list(q.options),
            "answer_index": q.answer_index, "explanation": q.explanation,
            "diagram": q.diagram, "source_key": q.key,
            "evidence": [{"path": e.path, "start_line": e.start_line,
                          "end_line": e.end_line, "quote": e.quote} for e in q.evidence]}


def from_raw(raws: list[dict]) -> list[Question]:
    """`to_raw`의 역 — 저장된 회차를 복원한다. 키는 저장된 것을 그대로 쓴다."""
    keys = {r.get("source_key") for r in (raws or []) if isinstance(r, dict)}
    return parse_questions(raws, trusted_keys={k for k in keys if isinstance(k, str)})
