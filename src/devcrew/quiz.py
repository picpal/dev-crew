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


def parse_questions(raw: list[dict],
                    allowed_keys: set[str] | None = None) -> list[Question]:
    """모델 출력 → 문항. 형태가 어긋난 항목은 조용히 버린다.

    재출제 문항은 `source_key`로 **원래 키를 이월**한다. 키를 매번 재계산하면 모델이
    같은 지점을 40–52로 인용했다가 38–55로 인용하는 순간 오답 해소가 끊긴다.
    다만 이월은 **하네스가 발급한 키**(`allowed_keys`)일 때만 받는다 — 모델이 키를
    지어내도 남의 오답을 해소하지 못한다.

    근거가 **비어 있는** 문항은 여기서 버리지 않는다 — 인용 대조가 사유와 함께 버려야
    사용자에게 "왜 문항이 줄었는지" 설명할 수 있다."""
    allowed_keys = allowed_keys or set()
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
        src = r.get("source_key")
        carried = src if isinstance(src, str) and src in allowed_keys else None
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
    target = (root / ev.path).resolve()
    if not target.is_relative_to(root):        # `..` 로 저장소 밖을 가리킴
        return "path-outside-repo"
    if not target.is_file():
        return "path-missing"
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return "path-unreadable"
    start = max(1, ev.start_line)
    end = min(len(lines), ev.end_line if ev.end_line >= start else start)
    if start > len(lines):
        return "quote-not-in-range"
    # 모델은 줄바꿈·들여쓰기를 흘린다. 공백 차이로 진짜 근거를 버리면 안 되므로
    # 양쪽을 한 줄로 접어 비교한다 — 인용의 존재만 보고 형식은 보지 않는다.
    window = _norm(" ".join(lines[start - 1:end]))
    quote = _norm(ev.quote)
    if not quote or quote not in window:
        return "quote-not-in-range"
    return None


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
        reason = next((r for e in q.evidence if (r := _check_evidence(e, root))), None)
        (dropped.append((q, reason)) if reason else passed.append(q))
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
MISS_EVENT = "QuizMissEvent"
CLEARED_EVENT = "QuizClearedEvent"
ISSUED_EVENT = "QuizIssuedEvent"
ANSWER_EVENT = "QuizAnswerEvent"


def note_id(user: str, repo: str) -> str:
    """오답 노트의 execution_id — 사용자·repo 단위. 취약 영역은 사람마다 다르다."""
    return f"TUTOR-{user}-{repo}"


def open_misses(trace, exec_id: str) -> list[dict]:
    """지금 열려 있는 오답 — miss 뒤에 같은 key의 clear가 **없는** 것.

    trace는 append-only라 상태가 아니라 이력이다. 벽시계가 아니라 **id 순서**로 접는다
    (같은 초에 여러 이벤트가 들어오면 ts로는 순서가 갈리지 않는다)."""
    try:
        evs = trace.events(execution_id=exec_id)
    except Exception:
        return []
    state: dict[str, dict] = {}
    for e in sorted(evs, key=lambda e: e["id"]):
        key = (e.get("payload") or {}).get("q_key")
        if not key:
            continue
        if e["event_type"] == MISS_EVENT:
            state[key] = e["payload"]
        elif e["event_type"] == CLEARED_EVENT:
            state.pop(key, None)
    return list(state.values())


def record_scorecard(trace, exec_id: str, card: Scorecard,
                     prior_keys: set[str]) -> tuple[int, int]:
    """채점 결과를 오답 노트에 반영한다. 반환: (새 오답 수, 해소 수).

    해소는 **이전에 열려 있던 문항을 맞혔을 때만** 기록한다. 처음 맞힌 문항까지
    clear로 남기면 이력이 의미를 잃는다."""
    added = cleared = 0
    for r in card.results:
        k = r.question.key
        if not r.correct:
            trace.append(MISS_EVENT, task_id=exec_id, execution_id=exec_id,
                         payload={"q_key": k, "area": r.question.area,
                                  "stem": r.question.stem,
                                  "evidence": [vars(e) for e in r.question.evidence]})
            added += 1
        elif k in prior_keys:
            trace.append(CLEARED_EVENT, task_id=exec_id, execution_id=exec_id,
                         payload={"q_key": k, "area": r.question.area})
            cleared += 1
    return added, cleared


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
    return parse_questions(raws, allowed_keys={k for k in keys if isinstance(k, str)})
