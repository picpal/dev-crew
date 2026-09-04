"""@tutor — 학습 퀴즈 봇 (#19). Slack 스레드 1개 = 회차 1개.

흐름: `@tutor <repo>:` 멘션 → 출제 파이프라인(`tutor.issue_quiz`) → 스레드에서 1문항씩
버튼으로 → 10문항을 다 풀면 채점 → 오답 노트 갱신 → 카드 리포트 URL.

**진행 중에는 정답을 공개하지 않는다.** 문항마다 맞았다/틀렸다를 알려주면 마지막 리포트가
할 일이 없어지고, 사용자는 문항이 아니라 피드백을 좇게 된다.

상태는 메모리 세션이 아니라 **trace가 진실**이다. 출제 직후 회차 전체를 `QuizIssuedEvent`로,
답변마다 `QuizAnswerEvent`로 남기므로 프로세스가 죽어도 스레드에서 이어 풀 수 있다.
"""
from __future__ import annotations

import asyncio
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from .quiz import (ANSWER_EVENT, ISSUED_EVENT, QUESTION_EVENT,
                   REPORT_FAILED_EVENT, ROUND_GRADED_EVENT, TOPIC_EVENT,
                   TA_ANSWER_EVENT, NoteUnavailable, Question, Scorecard,
                   from_raw, grade, note_id, open_misses, record_scorecard, to_raw)
from .report.code_report import render_code_report
from .report.quiz_report import (render_quiz_report, render_research_report,
                                 render_ta_answer)
from .report.uploader import publish_report
from .repos import RepoRegistryError, format_repo_names, split_repo_prefix
from .slack_brain import to_mrkdwn
from .tutor import issue_quiz
from .tutor_code import trace_code
from .tutor_research import ResearchError, research
from .tutor_vis import draw
from .tutor_ta import (SLACK_LIMIT, TRUNCATED_NOTE, TutorTAError, ask,
                       round_context, slack_head)

_MENTION_RE = re.compile(r"<@[A-Z0-9]+>")
_WS_RE = re.compile(r"\s+")
_MD_MARKS_RE = re.compile(r"[*_`~#]")
ROUND_TTL = 24 * 3600.0     # 미완 회차의 수명 — 하루가 지나면 코드도 기억도 달라진다
# 후속 질문 세션(TUTOR_TA)의 반납 규칙. 스레드당 **살아 있는 워커 프로세스** 하나이므로
# 반납은 선택이 아니다 — slack_brain(같은 모양의 스레드당 대화형 세션)과 같은 값을 쓴다.
TA_IDLE_TTL = 6 * 3600.0    # 마지막 질문 뒤 이만큼 방치되면 세션을 반납한다
MAX_TA_SESSIONS = 50        # 초과 시 가장 오래 방치된 회차부터 축출
# 유휴 세션을 쓸어담는 주기. 축출이 **답변 직후에만** 돌던 시절에는 마지막 질문 뒤
# 아무도 안 물으면 TTL이 지나도 세션이 남았다 — 17시간 산 워커를 실제로 발견했다
# (2026-08-25). TTL(6h)에 비해 촘촘하지만, 하는 일이 dict 훑기라 비용이 없다.
SWEEP_INTERVAL = 600.0
ANSWER_RE = re.compile(r"^(\d+):([0-3])$")
REGRADE_VALUE = "__REGRADE__"   # 채점 실패 후 다시 채점하는 버튼의 sentinel
# 주제 조사 리포트 아래의 출제 버튼 (§10.8). **기본 산출물은 리포트이고 출제는 선택**이다
# — 조사 직후 자동으로 문항을 내면 학습자는 읽지도 않은 자료로 시험을 본다.
START_QUIZ_VALUE = "__START_QUIZ__"
# 회차 전용 자료가 사는 곳. repo가 아니므로 registry·worktree·병합과 무관하다.
DEFAULT_CORPUS_ROOT = Path(".devcrew-runtime") / "corpus"
LETTERS = "ABCDEFGH"
BAR_FULL, BAR_EMPTY = "▰", "▱"
TYPE_HINT = {"CORRECT": ("✅", "옳은 것"), "INCORRECT": ("⛔", "틀린 것")}
AREA_LINES = 12          # 채점 결과에 펼치는 영역 줄 수 상한
NEED_REPO = ("⚠️ 대상 repo를 지정해 주세요 — `@tutor <repo명>: ` 형식입니다.\n"
             "등록된 repo: {repos}")
# 멘션만 하고 아무것도 안 적었을 때. **두 모드를 다 알려준다** — 접두 없는 문장은
# 이제 오류가 아니라 주제 학습이다 (§10.8).
NEED_INPUT = ("🎓 무엇을 학습할까요?\n"
              "• 주제를 적으면 조사해서 학습 리포트를 만듭니다 — `@tutor Kafka 리밸런싱`\n"
              "• 코드 이해도를 보려면 repo를 지정하세요 — `@tutor <repo명>: `\n"
              "등록된 repo: {repos}")
STALE_MSG = ("⚠️ 이 회차는 하루가 지나 이어서 풀 수 없습니다. "
             "`@tutor <repo명>:` 로 다시 시작해 주세요.")
NOT_OWNER = "⚠️ 이 회차를 시작한 사람만 답할 수 있습니다."
NEED_GRADED = "⚠️ 회차가 진행 중입니다 — 채점이 끝난 뒤에 물어봐 주세요."
TA_BUSY = "⏳ 앞선 질문에 답하는 중입니다. 끝나면 이어서 답합니다."
TA_FAIL = "💥 답변에 실패했습니다: {reason}. 같은 질문을 다시 물어봐 주세요."
NO_REPO = ("⚠️ 이 회차의 repo `{repo}` 를 더 이상 찾을 수 없어 답하지 않습니다 — "
           "다른 repo의 코드를 근거로 답하게 됩니다.\n등록된 repo: {repos}")


def round_id(thread_ts: str) -> str:
    """회차의 execution_id — 스레드 단위. 오답 노트(사용자·repo 단위)와는 다른 축이다."""
    return f"QUIZ-{thread_ts}"


@dataclass
class QuizSession:
    channel: str
    thread_ts: str
    owner: str
    repo_name: str
    questions: list[Question]
    # 주제 모드(§10.8)의 자료 디렉토리. repo가 아니라 회차에 매달린 임시 공간이라
    # registry로 풀 수 없다 — 근거 경로를 여기서 직접 들고 있어야 한다.
    corpus_dir: str | None = None
    answers: dict[int, int] = field(default_factory=dict)
    done: bool = False
    ta_session_id: str | None = None    # 후속 질문 대화 세션 (#19)
    ta_provider: object | None = None
    ta_instance_id: str | None = None   # 반납용 — registry.finish는 이것으로만 한다
    ta_busy: bool = False               # 답변 중 — 축출 대상에서 뺀다
    ta_touched: float = field(default_factory=time.monotonic)
    warned: bool = False                # 진행 중 안내를 이미 보냈는가
    started_at: float = field(default_factory=time.time)


def escape_slack(text: str) -> str:
    """`<`·`>`·`&`는 Slack이 링크·엔티티 문법으로 먹는다. 코드 조각을 그대로 보기에
    넣는 문항이라 이걸 안 걷으면 `Callable[<...>]` 같은 보기가 통째로 사라진다."""
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def rich(text: str, limit: int = 2900) -> str:
    """모델이 쓴 표준 마크다운 → Slack mrkdwn.

    출제 모델은 `**굵게**`로 쓰지만 Slack은 별 하나(`*굵게*`)만 굵게 그린다. 변환하지
    않으면 지문의 강조가 전부 `**어긋나는**` 같은 날문자로 보인다."""
    out = to_mrkdwn(escape_slack(text)).strip()
    return out if len(out) <= limit else out[:limit - 1].rstrip() + "…"


def plain(text: str, limit: int = 150) -> str:
    """header·버튼용 — `plain_text`에는 마크다운이 없어 기호가 그대로 찍힌다."""
    out = _WS_RE.sub(" ", _MD_MARKS_RE.sub("", text or "")).strip()
    return out if len(out) <= limit else out[:limit - 1].rstrip() + "…"


def bar(done: int, total: int, width: int = 10) -> str:
    """진행·정답률 막대. Slack에는 진행 표시가 없어 글자로 그린다."""
    if total <= 0:
        return ""
    filled = max(0, min(width, round(width * done / total)))
    return BAR_FULL * filled + BAR_EMPTY * (width - filled)


def question_blocks(q: Question, idx: int, total: int) -> list[dict]:
    """문항 하나 — 진행 막대 / 영역 제목 / 지문 / 보기 4개 / 버튼. 정오는 표시하지 않는다.

    보기는 **한 보기당 한 섹션**이다. 넷을 한 덩어리에 넣으면 100자짜리 보기 넷이
    문단처럼 이어 붙어 A·B·C·D 경계가 사라진다 — 읽는 사람이 지금 몇 번을 읽고 있는지
    모르는 것이 이 화면의 가장 큰 문제였다."""
    emoji, kind = TYPE_HINT.get(q.type, ("•", "알맞은 것"))
    blocks: list[dict] = [
        {"type": "context", "elements": [{"type": "mrkdwn",
         "text": f"{bar(idx, total)}  *{idx + 1} / {total}*"}]},
        {"type": "header", "text": {"type": "plain_text",
                                    "text": plain(f"Q{idx + 1}. {q.area}"), "emoji": True}},
        {"type": "section", "text": {"type": "mrkdwn", "text": rich(q.stem)}},
        {"type": "context", "elements": [{"type": "mrkdwn",
         "text": f"{emoji} 아래 보기에서 *{kind}* 하나를 고르세요"}]},
        {"type": "divider"},
    ]
    blocks += [{"type": "section", "text": {"type": "mrkdwn",
                "text": f"*{LETTERS[i]}*  ·  {rich(o, 1200)}"}}
               for i, o in enumerate(q.options)]
    blocks += [
        {"type": "actions", "block_id": "tutor_answers", "elements": [
            {"type": "button", "action_id": f"tutor_answer_{idx}_{i}",
             "text": {"type": "plain_text", "text": LETTERS[i]},
             "value": f"{idx}:{i}"} for i in range(len(q.options))]},
        {"type": "context", "elements": [{"type": "mrkdwn",
         "text": "_채점과 해설은 회차를 마친 뒤 리포트에서 한 번에 봅니다_"}]},
    ]
    return blocks


def research_blocks(topic: str, res, url: str | None) -> list[dict]:
    """조사 리포트 안내 — 첫 문단 + 📄 링크 + 출제 버튼 (§10.8).

    **본문 전체를 스레드에 쏟지 않는다.** 첫 문단은 프롬프트가 요약으로 쓰게 돼 있고,
    나머지는 리포트가 받는다 — 링크는 "읽고 넘어갈 결론"이라는 신호이기도 하다.

    출제 버튼은 자료가 실제로 남았을 때만 붙인다. 자료가 없으면 눌러도 근거 없는
    회차가 되므로, 누를 수 있는 것처럼 보이게 두지 않는다.
    """
    head = (res.report or "").strip().split("\n\n")[0].strip()
    blocks = [
        {"type": "header", "text": {"type": "plain_text",
                                    "text": plain(f"🎓 {topic}", 150), "emoji": True}},
        {"type": "section", "text": {"type": "mrkdwn", "text": rich(head, 2500)}},
    ]
    notes = list(res.notes)
    if res.sources:
        notes.append(f"🔗 출처 {len(res.sources)}건 · 자료 {len(res.files)}개")
    if notes:
        blocks.append({"type": "context",
                       "elements": [{"type": "mrkdwn", "text": "\n".join(notes)}]})
    if url:
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
                       "text": f"📄 <{url}|전체 리포트 열기>"}})
    if res.files:
        blocks.append({"type": "actions", "block_id": "tutor_start_quiz", "elements": [
            {"type": "button", "action_id": "tutor_answer_start_quiz", "style": "primary",
             "text": {"type": "plain_text", "text": "🎯 이해도 확인", "emoji": True},
             "value": START_QUIZ_VALUE}]})
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
                       "text": "_리포트를 읽고 나서 눌러 주세요 — 이 자료로 10문항을 냅니다_"}]})
    return blocks


def research_text(topic: str, res, url: str | None) -> str:
    """블록을 못 그리는 클라이언트·알림 미리보기용 대체 텍스트."""
    head = (res.report or "").strip().split("\n\n")[0].strip()
    out = f"🎓 {topic}\n{head}"
    if url:
        out += f"\n📄 {url}"
    return out


def opening_blocks(repo_name: str, total: int, *, shortfall: bool, misses: int) -> list[dict]:
    """회차 시작 안내. 사유(부족 출제·이월 오답)는 본문이 아니라 context로 내린다 —
    회차마다 붙는 곁줄이 제목만큼 커 보이면 정작 몇 문항인지가 안 읽힌다."""
    notes = []
    if shortfall:
        notes.append(f"⚠️ 근거를 찾지 못해 {total}문항만 출제했습니다")
    if misses:
        notes.append(f"📌 지난 오답 {misses}건을 노트에서 함께 보고 있습니다")
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "🎓 학습 회차 시작",
                                    "emoji": True}},
        {"type": "section", "text": {"type": "mrkdwn",
         "text": f"`{plain(repo_name, 80)}`  ·  총 *{total}*문항"}},
    ]
    if notes:
        blocks.append({"type": "context",
                       "elements": [{"type": "mrkdwn", "text": "\n".join(notes)}]})
    return blocks


def opening_text(repo_name: str, total: int, *, shortfall: bool, misses: int) -> str:
    """블록을 못 그리는 클라이언트·알림 미리보기용 대체 텍스트."""
    head = f"🎓 `{repo_name}` 학습 회차 — 총 {total}문항"
    if shortfall:
        head += f"\n⚠️ 근거를 찾지 못해 {total}문항만 출제했습니다."
    if misses:
        head += f"\n📌 지난 오답 {misses}건을 노트에서 보고 있습니다."
    return head


def score_blocks(card: Scorecard, *, repo_name: str, url: str | None,
                 added: int, cleared: int) -> list[dict]:
    """채점 결과 — 총점 / 영역별 막대 / 노트 변화 / 리포트 버튼.

    영역별을 `A 2/3 · B 1/2 · C 0/2`처럼 한 줄로 이어 붙이면 **어디가 약한지**가
    안 보인다. 이 화면의 쓸모는 점수가 아니라 약한 영역이므로 줄을 나누고 막대를 준다."""
    pct = round(card.correct * 100 / card.total) if card.total else 0
    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": "🎯 채점 완료",
                                    "emoji": True}},
        {"type": "section", "text": {"type": "mrkdwn",
         "text": (f"`{plain(repo_name, 80)}`  ·  *{card.correct} / {card.total}*  ({pct}%)\n"
                  f"{bar(card.correct, card.total)}")}},
    ]
    if card.by_area:
        # 글자 수로 자르면 마지막 줄이 문장 도중에 끊겨 고장난 것처럼 보인다.
        # 줄 단위로 자르고, 자른 사실을 숨기지 않는다.
        shown = list(card.by_area.items())[:AREA_LINES]
        lines = [f"{bar(ok, n, 5)}  {plain(area, 40)}  ·  *{ok}/{n}*"
                 for area, (ok, n) in shown]
        if len(card.by_area) > AREA_LINES:
            lines.append(f"_… 외 {len(card.by_area) - AREA_LINES}개 영역은 리포트에서_")
        blocks += [{"type": "divider"},
                   {"type": "section", "text": {"type": "mrkdwn",
                    "text": "*영역별*\n" + "\n".join(lines)}}]
    if added or cleared:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
                       "text": f"📕 오답 노트 · 새로 담음 *{added}* · 해소 *{cleared}*"}]})
    if url:
        blocks.append({"type": "actions", "elements": [
            {"type": "button", "style": "primary", "action_id": "tutor_report",
             "text": {"type": "plain_text", "text": "📄 해설 리포트 열기", "emoji": True},
             "url": url}]})
    else:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
                       "text": "_리포트 발행에 실패했습니다 — 점수만 전달합니다_"}]})
    return blocks


def report_blocks(body: str, url: str, label: str) -> list[dict]:
    """본문 + 리포트 **버튼**. 날 URL을 본문에 남기지 않는다 (사용자 2026-08-25).

    스레드에 URL을 흘리면 채점 리포트(이미 버튼)와 모양이 어긋나고, 모바일에서는
    눌러야 할 것인지도 덜 분명하다. `action_id`는 채점 리포트와 같은 `tutor_report`를
    쓴다 — 열기 전용 버튼이라 핸들러가 ack만 하면 되고, 새 id는 ack 배선을 하나 더
    만들 뿐이다(빠뜨리면 Slack에 "작업 실패"가 뜬다).
    """
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": rich(body, 2900)}},
        {"type": "actions", "elements": [
            {"type": "button", "style": "primary", "action_id": "tutor_report",
             "text": {"type": "plain_text", "text": plain(label, 70), "emoji": True},
             "url": url}]},
    ]


def score_text(card: Scorecard, *, url: str | None, added: int, cleared: int) -> str:
    lines = [f"✅ 채점 완료 — *{card.correct} / {card.total}*",
             " · ".join(f"{a} {ok}/{n}" for a, (ok, n) in card.by_area.items())]
    if added or cleared:
        lines.append(f"오답 노트: +{added} / 해소 {cleared}")
    lines.append(f"📄 리포트: {url}" if url
                 else "_(리포트 발행에 실패했습니다 — 점수만 전달합니다)_")
    return "\n".join(lines)


def regrade_blocks() -> list[dict]:
    """채점이 실패했을 때 주는 손잡이. 마지막 문항 버튼은 이미 걷힌 뒤라
    이걸 안 주면 사용자에게는 회차를 되살릴 방법이 없다."""
    return [
        {"type": "section", "text": {"type": "mrkdwn",
                                     "text": "채점을 마치지 못했습니다. 다시 시도할 수 있습니다."}},
        {"type": "actions", "block_id": "tutor_regrade", "elements": [
            {"type": "button", "action_id": "tutor_answer_regrade", "style": "primary",
             "text": {"type": "plain_text", "text": "🔁 다시 채점"},
             "value": REGRADE_VALUE}]},
    ]


class TutorHandler:
    """멘션 → 출제·회차 시작, 버튼 → 답변·진행, 완주 → 채점·리포트.

    `publish`는 리포트 업로드 함수 (테스트에서 대역으로 바꾼다)."""

    def __init__(self, orch, cfg, repos: dict, *, react=None, status=None,
                 update=None, publish=publish_report, max_seen: int = 1000,
                 corpus_root=None):
        self.orch = orch
        self.cfg = cfg
        self.repos = repos
        # 회차 전용 자료가 사는 뿌리. **본체 저장소 아래**여야 한다 — worktree처럼
        # 회수되는 공간에 두면 회차보다 자료가 먼저 사라진다 (lessons C13).
        self.corpus_root = Path(corpus_root or DEFAULT_CORPUS_ROOT)
        self.react = react
        self.status = status
        self.update = update      # async (channel, ts, text, blocks) — 진행 표시 갱신
        self.publish = publish
        self.sessions: dict[str, QuizSession] = {}
        self.last_questions: list[Question] = []   # 최근 회차 (운영·테스트 편의)
        self._seen: set[str] = set()
        self._max_seen = max_seen
        self._opening: set[str] = set()
        self._lock = asyncio.Lock()
        self._ta_locks: dict[str, asyncio.Lock] = {}
        self._expired: set[str] = set()      # 만료 안내를 이미 보낸 스레드
        # 조사만 끝나고 아직 출제하지 않은 스레드 (§10.8). 출제 버튼이 대상을 찾는
        # 자리이고, 프로세스가 죽어도 `TOPIC_EVENT`로 되살린다.
        self.topics: dict[str, dict] = {}

    # ── 진입점 ──────────────────────────────────────────────────────────────
    async def on_mention(self, body: dict, say) -> None:
        if self._dedupe(body):
            return
        event = body.get("event") or {}
        thread_ts = event.get("thread_ts") or event.get("ts")
        channel, user = event.get("channel", ""), event.get("user") or ""
        text = _MENTION_RE.sub("", event.get("text") or "").strip()
        try:
            repo_name, _rest = split_repo_prefix(text, self.repos)
        except RepoRegistryError as e:
            await say(text=f"⚠️ {e}", thread_ts=thread_ts)
            return
        if thread_ts in self.sessions or thread_ts in self._opening:
            return
        if not repo_name:
            # **주제 모드** (§10.8). 접두가 없으면 repo가 아니라 배우고 싶은 주제다 —
            # 조사해서 학습 리포트를 내고, 출제는 그다음에 버튼으로 고르게 한다.
            topic = _rest.strip()
            if not topic:
                await say(text=NEED_INPUT.format(repos=format_repo_names(self.repos)),
                          thread_ts=thread_ts)
                return
            self._opening.add(thread_ts)
            try:
                await self._ack(event)
                await self._research(thread_ts, channel, user, topic, say)
            finally:
                self._opening.discard(thread_ts)
            return
        self._opening.add(thread_ts)
        try:
            await self._ack(event)
            await self._set_status(channel, thread_ts, "출제 중…")
            await self._start(thread_ts, channel, user, repo_name, say)
        finally:
            self._opening.discard(thread_ts)

    async def on_answer(self, *, thread_ts: str, value: str, say, strip=None,
                        channel: str = "", user: str = "") -> None:
        if value == START_QUIZ_VALUE:
            # 조사 리포트의 출제 버튼 — 아직 회차가 없으므로 세션 조회보다 **앞**이다.
            # 버튼은 먼저 걷는다: 출제는 수 분이 걸리고, 그동안 다시 눌리면 같은
            # 스레드에 회차가 둘 열린다.
            if thread_ts in self.sessions or thread_ts in self._opening:
                return
            self._opening.add(thread_ts)
            try:
                if strip:
                    try:
                        await strip()
                    except Exception:
                        pass
                await self._start_from_topic(thread_ts, channel, user, say)
            finally:
                self._opening.discard(thread_ts)
            return
        regrade = value == REGRADE_VALUE
        m = ANSWER_RE.match(value or "")
        if not m and not regrade:
            return
        sess = self.sessions.get(thread_ts) or await self._resume(thread_ts, say)
        if sess is None:
            return
        if sess.owner and user != sess.owner:
            # 대화는 누구나 볼 수 있지만 답은 회차 주인의 것이다 — 남이 채점을 흔들면 안 된다
            await say(text=NOT_OWNER, thread_ts=thread_ts)
            return
        if sess.done:
            return
        if regrade:
            if strip:
                try:
                    await strip()
                except Exception:
                    pass
            await self._finish(sess, say)
            return
        idx, choice = int(m.group(1)), int(m.group(2))
        if not 0 <= idx < len(sess.questions):
            return
        # 버튼을 먼저 걷으면 기록 실패 시 그 답을 다시 낼 방법이 없다 (lessons.md C7).
        try:
            self.orch.trace.append(ANSWER_EVENT, task_id=round_id(thread_ts),
                                   execution_id=round_id(thread_ts),
                                   payload={"index": idx, "choice": choice})
        except Exception as e:
            await say(text=f"💥 답변 기록에 실패했습니다: {type(e).__name__}. "
                           "같은 버튼을 다시 눌러 주세요.", thread_ts=thread_ts)
            return
        sess.answers[idx] = choice
        if strip:
            try:
                await strip()
            except Exception:
                pass
        nxt = self._next_index(sess)
        if nxt is None:
            await self._finish(sess, say)
            return
        await self._post_question(sess, nxt, say)

    def has_round(self, thread_ts: str) -> bool:
        """이 스레드에 회차가 있는가 — 메모리에 없으면 trace에 물어본다.

        라우팅 결정(질문이냐 새 회차냐)에만 쓴다. 메모리만 보면 재시작 뒤에 채점이
        끝난 스레드에서 새 회차가 열린다 — 회차의 진실은 trace다. TTL·채점 여부는
        여기서 보지 않는다: 그 판정은 `on_question`이 세션을 복원한 뒤에 하고,
        여기서 겹쳐 보면 만료된 회차의 스레드가 새 회차 시작 경로로 새어 나간다.
        """
        if thread_ts in self.sessions:
            return True
        try:
            return any(e["event_type"] == ISSUED_EVENT
                       for e in self.orch.trace.events(execution_id=round_id(thread_ts)))
        except Exception:
            return False

    async def on_question(self, *, thread_ts: str, text: str, user: str, say,
                          channel: str = "") -> None:
        """채점이 끝난 회차에 대한 후속 질문 (#19).

        해설을 읽고도 막힌 지점을 푸는 것이 목적이므로 **채점 후에만** 받는다.
        진행 중에 받으면 "3번 보기 B가 왜 틀려?"가 형식상 질문인 채로 정답을 흘린다.
        """
        question = (text or "").strip()
        if not question:
            return
        sess = self.sessions.get(thread_ts) or await self._resume(thread_ts, say)
        if sess is None:
            return                       # 회차가 없는 스레드 — 우리 일이 아니다
        if not sess.done:
            if not sess.warned:
                sess.warned = True       # 답글마다 안내하면 알림이 아니라 잔소리다
                await say(text=NEED_GRADED, thread_ts=thread_ts)
            return
        if not sess.owner or user != sess.owner:
            # **fail-closed.** owner가 빈 문자열이면 `sess.owner and ...`는 통째로
            # False가 되어 누구나 통과했다 — `_resume`이 `payload.get("owner", "")`로
            # 복원하므로 필드가 없던 옛 회차·손상된 페이로드가 곧바로 개방이 된다.
            # 답변에는 정답·근거·해설이 그대로 들어가므로 실패 방향은 닫힘이어야 한다.
            await say(text=NOT_OWNER, thread_ts=thread_ts)
            return
        if time.time() - sess.started_at > ROUND_TTL:
            # 하루가 지나면 코드도 기억도 달라진다. 그때의 근거로 답하는 것이 오히려
            # 틀린 설명이 된다. 메모리 세션이 살아 있어도 같은 규칙을 쓴다.
            await self._expire(thread_ts, say, sess=sess)
            return
        repo_path = self.evidence_root(sess)
        if not repo_path:
            # 빈 경로는 조용히 넘어가지 않는다: `spawn(worktree="")`는 작업 디렉토리
            # 고정 블록을 건너뛰어 워커가 **엔진 프로세스의 cwd**(= 하네스 자기 repo)에서
            # 뜨고, `Path("").resolve()`도 같은 곳이라 인용 대조까지 하네스 기준이 된다 —
            # 틀린 repo의 인용이 "대조 통과"로 표시된다. 거짓을 가르치느니 답하지 않는다.
            await say(text=NO_REPO.format(repo=plain(sess.repo_name, 60),
                                          repos=format_repo_names(self.repos)),
                      thread_ts=thread_ts)
            return
        lock = self._ta_lock_for(thread_ts)
        if lock.locked():
            await say(text=TA_BUSY, thread_ts=thread_ts)
        await self._set_status(channel or sess.channel, thread_ts, "답변 준비 중…")
        try:
            async with lock:
                # context는 세션을 새로 열 때만 쓰이지만 항상 계산해 넘긴다 — ask()는
                # 이 adapter 인스턴스가 session_id를 모르게 됐을 때(cross-process 재시작
                # 등) context가 있어야만 새 세션으로 자연 복구한다. 재사용 경로에서
                # None을 넘기면 그 복구 코드가 죽은 채로 남고, 세션을 잃은 학습자는
                # "새 질문으로 다시 시작해 주세요"만 영원히 본다. round_context 조립은
                # 순수 문자열 작업이라 매번 계산해도 비용은 없다.
                sess.ta_busy = True      # 답변 중인 회차는 축출하지 않는다
                try:
                    res = await ask(
                        self.orch, self.cfg, exec_id=round_id(thread_ts),
                        repo_path=repo_path, question=question,
                        session_id=sess.ta_session_id, provider=sess.ta_provider,
                        instance_id=sess.ta_instance_id,
                        context=round_context(sess.questions, sess.answers,
                                              repo_name=sess.repo_name))
                finally:
                    sess.ta_busy = False
        except TutorTAError as e:
            await say(text=TA_FAIL.format(reason=e), thread_ts=thread_ts)
            return
        sess.ta_session_id, sess.ta_provider = res.session_id, res.provider
        sess.ta_instance_id = res.instance_id
        sess.ta_touched = time.monotonic()
        # `ta_busy`는 lock을 잡은 뒤에야 서므로, 게이트 통과부터 그 줄까지(`_set_status`
        # 네트워크 호출 + lock 대기) 이 회차는 축출 후보로 남아 있다. 그 창에서 다른
        # 스레드의 답변이 `_evict_ta`를 돌려 이 회차를 `_drop`했다면 지금 `sess`는
        # sessions에서 떨어져 나온 객체다 — 그대로 두면 방금 연 세션의 손잡이가 아무
        # 데도 남지 않는다. 다시 등록해 그 창을 닫는다. (그 사이 같은 스레드에 새
        # 세션이 생겼다면 그쪽이 정본이므로 setdefault로 덮지 않는다.)
        self.sessions.setdefault(thread_ts, sess)
        await self._evict_ta()
        # **기록이 먼저다.** 기록에 실패했는데 답을 보이면, 사용자는 답을 봤는데 우리는
        # 무엇을 답했는지 모르는 상태가 된다 (lessons C7) — 그래서 기록이 실패하면
        # `on_answer`와 같은 원칙으로 답을 보이지 않고 실패를 알린다. 세션(ta_session_id)은
        # 이미 실제로 열렸으므로 되돌리지 않는다 — 같은 질문을 다시 물으면 그 세션을
        # 이어서 쓴다.
        try:
            self.orch.trace.append(QUESTION_EVENT, task_id=round_id(thread_ts),
                                   execution_id=round_id(thread_ts),
                                   payload={"user": user, "question": question})
            self.orch.trace.append(
                TA_ANSWER_EVENT, task_id=round_id(thread_ts),
                execution_id=round_id(thread_ts),
                payload={"answer": res.text, "dropped": res.dropped,
                         "status": res.status, "summary": res.summary,
                         "citations": [{"path": c.path, "start_line": c.start_line,
                                        "end_line": c.end_line} for c in res.citations]})
        except Exception as e:
            await say(text=f"💥 답변 기록에 실패했습니다: {type(e).__name__}. "
                           "같은 질문을 다시 물어봐 주세요.", thread_ts=thread_ts)
            return
        # 같은 파일의 문항은 전부 rich()를 거치는데 TA 답변만 원문으로 나가고 있었다 —
        # `**굵게**`가 날문자로 찍히고 `Callable[<T>]`의 `<T>`는 Slack이 엔티티로 먹어
        # 통째로 사라진다. 방어를 프롬프트("별 하나로 써라")에 맡기지 않는다.
        await self._post_answer(thread_ts, question, res, sess, say)

    async def _post_answer(self, thread_ts: str, question: str, res, sess,
                           say) -> None:
        """답변을 스레드에 낸다. 한 메시지에 안 들어가면 리포트로 흘린다.

        예전에는 SLACK_LIMIT에서 그냥 잘라 "답변이 길어 잘렸습니다"만 남았다 — 학습자는
        나머지를 볼 방법이 없었고, 모델이 제대로 쓴 설명의 뒷부분이 매번 버려졌다.

        **발행이 실패해도 답은 준다.** 링크를 못 만들었다고 답을 통째로 삼키면 모델은
        제대로 답했는데 학습자만 잃는다 — 잘라서라도 보내고 사유를 남긴다.
        """
        if res.code_focus:
            # 실행 흐름 질문이면 산문보다 좌우 분할 리포트가 낫다. **실패해도 답은
            # 준다** — 리포트는 곁다리지 답이 아니다.
            url = await self._code_report(thread_ts, question, res, sess)
            if url:
                head = slack_head(res.text, dropped=res.dropped)
                # `text`에도 URL을 남긴다 — blocks가 있으면 Slack은 이걸 **표시하지
                # 않고** 알림 미리보기와 폴백으로만 쓴다. `_say_blocks`는 blocks를 못
                # 받는 `say`에 대해 텍스트로 되돌아가므로, 빼면 그 경로에서 링크가
                # 통째로 사라진다.
                await self._say_blocks(
                    say, thread_ts, f"{head}\n\n▶️ 실행 흐름 보기: {url}",
                    report_blocks(head, url, "▶️ 실행 흐름 보기"))
                return
        if len(res.text) <= SLACK_LIMIT:
            await say(text=rich(res.text), thread_ts=thread_ts)
            return
        try:
            html = render_ta_answer(question=question, answer=res.text,
                                    citations=res.citations, repo=sess.repo_name,
                                    diagram=await self._diagram(thread_ts, res.diagram))
            url = await asyncio.to_thread(self.publish,
                                          self._ta_report_id(thread_ts, question), html)
        except Exception as e:
            try:
                self.orch.trace.append(REPORT_FAILED_EVENT, task_id=round_id(thread_ts),
                                       execution_id=round_id(thread_ts),
                                       payload={"reason": f"{type(e).__name__}: {e}",
                                                "kind": "ta_answer"})
            except Exception:
                pass
            url = None
        if not url:
            # rich()가 2900자에서 자른다 — 잘렸다는 사실은 말해 준다 (lessons C12).
            await say(text=rich(res.text) + TRUNCATED_NOTE, thread_ts=thread_ts)
            return
        head = slack_head(res.text, dropped=res.dropped)
        await self._say_blocks(say, thread_ts, f"{head}\n\n📄 전체 답변: {url}",
                               report_blocks(head, url, "📄 전체 답변 열기"))

    async def _code_report(self, thread_ts: str, question: str, res, sess) -> str | None:
        """실행 추적 → 리포트 발행 → URL. 어디서 실패하든 None을 돌려주고 사유만 남긴다.

        추적은 별도 세션(TUTOR_CODE)이다 — TA 스키마에 스텝까지 담으면 큰 필드가 둘이
        되어 C15가 돌아오고, 실행 추적은 질문 답변과 다른 과업이라 프롬프트도 갈려야 한다.
        """
        repo_path = self.evidence_root(sess)
        if not repo_path:
            return None
        try:
            tr = await trace_code(
                self.orch, self.cfg, exec_id=round_id(thread_ts), repo_path=repo_path,
                focus_path=res.code_focus.get("path"),
                focus_symbol=res.code_focus.get("symbol"), question=question)
            html = render_code_report(tr, question=question, repo=sess.repo_name,
                                      diagram=await self._diagram(thread_ts, tr.diagram))
            return await asyncio.to_thread(
                self.publish, self._code_report_id(thread_ts, question), html)
        except Exception as e:
            try:
                self.orch.trace.append(REPORT_FAILED_EVENT, task_id=round_id(thread_ts),
                                       execution_id=round_id(thread_ts),
                                       payload={"reason": f"{type(e).__name__}: {e}",
                                                "kind": "code_report"})
            except Exception:
                pass
            return None

    async def _diagram(self, thread_ts: str, spec: str | None) -> str | None:
        """`diagram` spec이 있으면 그려서 살균된 SVG를 돌려준다. 없거나 실패하면 None.

        **그림은 곁다리다.** 못 그렸다고 답변을 막지 않는다 — `draw()`가 조용히 None을
        내고 리포트는 그림 없이 나간다. 그리기는 별도 세션(TUTOR_VIS)이고 **임시
        디렉토리**에서 돈다: 쓰기가 필요한 유일한 tutor role이라, 사용자 repo를 cwd로
        받는 다른 role과 섞지 않는다.
        """
        if not spec:
            return None
        return await draw(self.orch, self.cfg, exec_id=round_id(thread_ts), spec=spec)

    @staticmethod
    def _code_report_id(thread_ts: str, question: str) -> str:
        import hashlib
        seed = f"code:{thread_ts}:{question}"
        return "code-" + hashlib.sha256(seed.encode()).hexdigest()[:10]

    @staticmethod
    def _ta_report_id(thread_ts: str, question: str) -> str:
        import hashlib
        seed = f"ta:{thread_ts}:{question}"
        return "ta-" + hashlib.sha256(seed.encode()).hexdigest()[:10]

    # ── 회차 ────────────────────────────────────────────────────────────────
    # ── 주제 모드 (§10.8) ───────────────────────────────────────────────────
    def _progress(self, channel: str, thread_ts: str, say):
        """진행 표시 — **한 메시지를 갱신한다.**

        단계마다 새 메시지를 쌓으면 스레드가 회차 내용보다 진행 로그로 길어지고,
        끝난 뒤에도 남는다. 마지막에는 이 메시지가 그대로 결과 카드가 된다.

        `update`가 없거나 실패하면 새 메시지를 올리는 종전 동작으로 떨어진다 —
        진행이 안 보이는 것이 중복보다 나쁘다 (침묵은 고장과 구분되지 않는다).
        """
        state: dict = {"ts": None}

        async def tick(stage: str = "", *, text: str = "", blocks=None) -> None:
            body = text or f"⏳ {stage}"
            if stage:
                await self._set_status(channel, thread_ts, stage)
            if state["ts"] and self.update:
                try:
                    await self.update(channel, state["ts"], body, blocks)
                    return
                except Exception:
                    pass
            res = await self._say_blocks(say, thread_ts, body, blocks)
            if state["ts"] is None and isinstance(res, dict):
                state["ts"] = res.get("ts")

        return tick

    async def _research(self, thread_ts, channel, user, topic, say) -> None:
        """주제를 조사해 자료를 남기고 학습 리포트를 낸다. **출제는 하지 않는다.**

        읽지 않은 자료로 시험을 보게 하지 않으려고 버튼으로 갈라 둔다 (§10.8 D2).
        """
        # **보이는 곳에 적는다.** `_set_status`는 AI 어시스턴트 스레드 전용이라 일반
        # 채널에서는 조용히 실패한다 — 조사는 웹을 오가느라 길고, 그 침묵은 고장과
        # 구분되지 않는다 (2026-08-24 출제에서 겪은 것과 같은 문제).
        _tick = self._progress(channel, thread_ts, say)

        if self._lock.locked():
            await say(text="⏳ 다른 요청을 처리하는 중입니다. 끝나면 이어서 시작합니다.",
                      thread_ts=thread_ts)
        corpus = self.corpus_dir_for(thread_ts)
        try:
            async with self._lock:
                res = await research(self.orch, self.cfg, exec_id=round_id(thread_ts),
                                     topic=topic, corpus_dir=corpus, progress=_tick)
        except ResearchError as e:
            await say(text=f"💥 조사에 실패했습니다: {e}", thread_ts=thread_ts)
            return
        except Exception as e:
            await say(text=f"💥 조사에 실패했습니다: {type(e).__name__}: {e}",
                      thread_ts=thread_ts)
            return

        await _tick("리포트 만드는 중…")
        html = render_research_report(
            topic=topic, report=res.report, citations=res.citations,
            sources=res.sources, files=res.files,
            diagram=await self._diagram(thread_ts, res.diagram))
        try:
            url = await asyncio.to_thread(self.publish,
                                          self._research_report_id(thread_ts), html)
        except Exception as e:
            # **발행 실패가 조사를 통째로 날리면 안 된다.** 자료는 이미 디스크에 있고
            # 본문도 손에 있다 — 링크만 없을 뿐이다. 링크 하나 때문에 10분짜리 조사와
            # 출제 버튼까지 잃는 것이 실제로 일어났다 (2026-09-04: wrangler 60초 상한).
            # TA 답변 경로가 이미 같은 처분을 하고 있었는데 이 경로만 빠져 있었다.
            try:
                self.orch.trace.append(REPORT_FAILED_EVENT, task_id=round_id(thread_ts),
                                       execution_id=round_id(thread_ts),
                                       payload={"reason": f"{type(e).__name__}: {e}",
                                                "kind": "research"})
            except Exception:
                pass
            url = None
            res.notes.append("리포트 발행에 실패해 링크가 없습니다 — 본문은 아래에 있습니다")
        # **기록이 먼저다.** 안내를 먼저 보내고 기록에 실패하면 버튼이 대상을 못 찾는다
        # (프로세스가 재시작되면 메모리 상태도 없다) — lessons C7.
        try:
            self.orch.trace.append(TOPIC_EVENT, task_id=round_id(thread_ts),
                                   execution_id=round_id(thread_ts),
                                   payload={"topic": topic, "corpus_dir": str(corpus),
                                            "owner": user, "channel": channel,
                                            "files": res.files, "url": url})
        except Exception as e:
            await say(text=f"💥 조사 결과 기록에 실패했습니다: {type(e).__name__}",
                      thread_ts=thread_ts)
            return
        self.topics[thread_ts] = {"topic": topic, "corpus_dir": str(corpus),
                                  "owner": user, "channel": channel}
        # 진행 메시지를 결과로 **갈아 끼운다** — 끝난 뒤에도 "⏳ 리포트 만드는 중…"이
        # 남아 있으면 무엇이 끝난 건지 알 수 없다.
        await _tick(text=research_text(topic, res, url),
                    blocks=research_blocks(topic, res, url))

    async def _start_from_topic(self, thread_ts, channel, user, say) -> bool:
        """조사한 자료로 회차를 연다 (출제 버튼). 대상을 못 찾으면 False.

        메모리에 없으면 trace에서 되살린다 — 리포트를 읽는 동안 브리지가 재시작되면
        버튼이 死문자가 되는데, 그건 사용자에게 "눌러도 아무 일이 없다"로만 보인다.
        """
        info = self.topics.get(thread_ts) or self._topic_from_trace(thread_ts)
        if not info:
            await say(text="⚠️ 이 스레드의 조사 자료를 찾지 못했습니다 — "
                           "주제를 다시 알려주시면 새로 조사하겠습니다.", thread_ts=thread_ts)
            return False
        if info.get("owner") and user != info["owner"]:
            await say(text=NOT_OWNER, thread_ts=thread_ts)
            return False
        corpus = info.get("corpus_dir") or ""
        if not corpus or not Path(corpus).is_dir():
            # 자료가 회수된 뒤다. 빈 경로로 출제하면 워커가 하네스 repo에서 뜨고
            # 엉뚱한 인용이 '대조 통과'로 표시된다 — 그럴 바에는 열지 않는다.
            await say(text="⚠️ 이 회차의 자료가 만료되어 문항을 낼 수 없습니다 — "
                           "같은 주제로 새로 물어봐 주세요.", thread_ts=thread_ts)
            return False
        await self._set_status(channel, thread_ts, "출제 중…")
        await self._start(thread_ts, channel, user, info["topic"], say,
                          corpus_dir=corpus)
        return True

    def _topic_from_trace(self, thread_ts: str) -> dict | None:
        try:
            evs = self.orch.trace.events(event_type=TOPIC_EVENT,
                                         execution_id=round_id(thread_ts))
        except Exception:
            return None
        if not evs:
            return None
        last = evs[-1]
        if time.time() - last["ts"] > ROUND_TTL:
            return None
        return last["payload"]

    @staticmethod
    def _research_report_id(thread_ts: str) -> str:
        import hashlib
        return "study-" + hashlib.sha256(f"study:{thread_ts}".encode()).hexdigest()[:10]

    def corpus_dir_for(self, thread_ts: str) -> Path:
        """이 회차의 자료 디렉토리. 회차 id와 1:1이라 정리도 이 이름으로 한다."""
        return self.corpus_root / round_id(thread_ts)

    def evidence_root(self, sess: QuizSession) -> str:
        """이 회차의 근거가 사는 곳 — 주제 모드면 corpus, repo 모드면 registry.

        **없으면 빈 문자열이다.** 빈 경로를 그대로 흘리면 `spawn(worktree="")`가
        작업 디렉토리 고정을 건너뛰어 워커가 엔진 프로세스의 cwd(= 하네스 자기 repo)
        에서 뜨고, 인용 대조까지 그 기준이 된다 — 틀린 근거가 '대조 통과'로 표시된다.
        그래서 호출자는 빈 값을 반드시 거절 경로로 처리한다.
        """
        if sess.corpus_dir:
            return sess.corpus_dir if Path(sess.corpus_dir).is_dir() else ""
        return str(self.repos.get(sess.repo_name) or "")

    async def _start(self, thread_ts, channel, user, repo_name, say,
                     corpus_dir: str | None = None) -> None:
        note = note_id(user, repo_name)
        try:
            misses = open_misses(self.orch.trace, note)
        except NoteUnavailable:
            # 노트를 못 읽은 채 회차를 열면 기존 오답이 재출제되지도, 맞혀도 해소되지도
            # 않는다. 사용자는 그 사실을 모른 채 10문항을 푼다 — 열지 않는 것이 낫다.
            await say(text="⚠️ 오답 노트를 읽지 못해 회차를 시작하지 않았습니다. "
                           "잠시 후 다시 시도해 주세요.", thread_ts=thread_ts)
            return
        if self._lock.locked():
            # 출제는 한 번에 하나만 돈다. 그 사실을 말해 주지 않으면 기다리는 쪽에는
            # 그냥 무반응으로 보이고, 사용자는 멘션을 반복하게 된다 (2026-08-24).
            await say(text="⏳ 다른 회차를 출제하는 중입니다. 끝나면 이어서 시작합니다.",
                      thread_ts=thread_ts)
        # **보이는 곳에 적는다.** `_set_status`는 `assistant_threads_setStatus`로 가는데
        # 그건 AI 어시스턴트 스레드 전용이라 일반 채널 스레드에서는 실패하고 조용히
        # 삼켜진다 — 코드는 진행을 알린다고 믿었지만 사용자 화면은 완전히 비어 있었다.
        # 출제는 실측 18분까지 걸린다. 그 침묵은 고장과 구분되지 않아 멘션을 반복하게 된다.
        async def _tick(stage: str) -> None:
            await self._set_status(channel, thread_ts, stage)
            await say(text=f"⏳ {stage}", thread_ts=thread_ts)

        try:
            async with self._lock:
                res = await issue_quiz(self.orch, self.cfg, repo_name=repo_name,
                                       repo_path=corpus_dir or str(self.repos[repo_name]),
                                       exec_id=round_id(thread_ts), misses=misses,
                                       progress=_tick)
        except Exception as e:
            await say(text=f"💥 출제에 실패했습니다: {type(e).__name__}: {e}",
                      thread_ts=thread_ts)
            return
        if not res.questions:
            # 사유 없이 "못 만들었습니다"만 보내면 사용자도 나도 원인을 못 찾는다
            detail = ("\n" + "\n".join(f"• {n}" for n in res.notes)) if res.notes \
                else "\n• 사유가 기록되지 않았습니다 — 브리지 로그를 확인해 주세요"
            await say(text=f"🙋 근거 있는 문항을 만들지 못했습니다.{detail}",
                      thread_ts=thread_ts)
            return
        total = len(res.questions)
        await _tick(f"회차 게시 중… ({total}문항)")
        # **기록이 먼저다.** 안내를 먼저 보내고 기록에 실패하면 사용자에게는 시작했다는
        # 메시지만 남고 이어 풀 수도, 되살릴 수도 없는 회차가 된다 (lessons.md C7).
        try:
            self.orch.trace.append(ISSUED_EVENT, task_id=round_id(thread_ts),
                                   execution_id=round_id(thread_ts),
                                   payload={"questions": [to_raw(q) for q in res.questions],
                                            "owner": user, "repo_name": repo_name,
                                            "channel": channel,
                                            "corpus_dir": corpus_dir})
        except Exception as e:
            await say(text=f"💥 회차 기록에 실패해 시작하지 않았습니다: {type(e).__name__}",
                      thread_ts=thread_ts)
            return
        # 숫자를 채우려 지어내지 않는다. 왜 적은지 사실대로 말한다 (#19 D6)
        opening = dict(shortfall=bool(res.shortfall), misses=len(misses))
        await self._say_blocks(say, thread_ts,
                               opening_text(repo_name, total, **opening),
                               opening_blocks(repo_name, total, **opening))
        sess = QuizSession(channel=channel, thread_ts=thread_ts, owner=user,
                           repo_name=repo_name, questions=res.questions,
                           corpus_dir=corpus_dir)
        self.sessions[thread_ts] = sess
        self.last_questions = res.questions
        await self._post_question(sess, 0, say)

    @staticmethod
    async def _say_blocks(say, thread_ts, text: str, blocks: list[dict] | None = None):
        """블록 게시. **응답을 돌려준다** — 진행 표시가 갱신할 `ts`를 알아야 한다."""
        try:
            return await say(text=text, thread_ts=thread_ts, blocks=blocks)
        except TypeError:
            return await say(text=text, thread_ts=thread_ts)

    async def _post_question(self, sess: QuizSession, idx: int, say) -> None:
        q = sess.questions[idx]
        total = len(sess.questions)
        # 대체 텍스트도 알림 미리보기로 읽히므로 마크다운을 그대로 흘리지 않는다
        text = f"*{idx + 1} / {total}* · {plain(q.area, 40)}\n{rich(q.stem, 300)}"
        await self._say_blocks(say, sess.thread_ts, text,
                               question_blocks(q, idx, total))

    async def _finish(self, sess: QuizSession, say) -> None:
        note = note_id(sess.owner, sess.repo_name)
        try:
            prior = {m["q_key"] for m in open_misses(self.orch.trace, note)}
        except NoteUnavailable:
            # done을 세우기 전에 빠진다 — 세우면 재시도가 거부돼 회차가 채점 없이 끝난다.
            # 마지막 문항 버튼은 이미 걷혔으므로 새 손잡이를 준다.
            await self._say_blocks(say, sess.thread_ts,
                                   "⚠️ 오답 노트를 읽지 못해 채점을 마치지 못했습니다.",
                                   regrade_blocks())
            return
        card = grade(sess.questions, sess.answers)
        # **기록이 먼저, done은 나중.** done을 먼저 세우면 기록이 실패했을 때 회차가
        # 영구히 채점 불가가 된다. 노트는 key 단위로 접히므로 재시도는 멱등이다.
        try:
            added, cleared = record_scorecard(self.orch.trace, note, card,
                                              prior_keys=prior)
        except Exception as e:
            await self._say_blocks(
                say, sess.thread_ts,
                f"⚠️ 채점 결과를 기록하지 못했습니다: {type(e).__name__}",
                regrade_blocks())
            return
        sess.done = True
        # 회차 자신의 execution_id에도 채점 완료를 남긴다 — `_resume`이 "이 회차가"
        # 채점됐는지 판정할 유일하게 안전한 근거다(위 GRADED_EVENT는 note_id 네임스페이스
        # 라 회차를 특정 못 한다). 기록이 실패해도 채점 자체(record_scorecard)는 이미
        # 끝났으니 여기서 되돌리지 않는다 — 다만 이 마커가 없으면 다음 재시작 때
        # fail-closed로 done=False가 되어 다시 완주해야 한다(안전한 실패 방향).
        try:
            self.orch.trace.append(ROUND_GRADED_EVENT, task_id=round_id(sess.thread_ts),
                                   execution_id=round_id(sess.thread_ts), payload={})
        except Exception:
            pass
        url, why = None, None
        try:
            html = render_quiz_report(card, repo=sess.repo_name, added=added,
                                      cleared=cleared)
            url = await asyncio.to_thread(self.publish, self._report_id(sess), html)
        except Exception as e:
            # **사유를 삼키지 않는다.** 2026-08-24 16:29 실제로 발행이 실패했는데
            # `except: url = None`이 사유를 통째로 버려, 나중에 채점·렌더·업로드를 전부
            # 다시 확인하고도 원인을 알 수 없었다. 채점은 이미 끝났으므로 회차를
            # 되돌리지는 않는다 — 링크만 없이 가고, 왜 없는지는 남긴다.
            url, why = None, f"{type(e).__name__}: {e}"
            try:
                self.orch.trace.append(REPORT_FAILED_EVENT,
                                       task_id=round_id(sess.thread_ts),
                                       execution_id=round_id(sess.thread_ts),
                                       payload={"reason": why})
            except Exception:
                pass
        await self._say_blocks(
            say, sess.thread_ts,
            score_text(card, url=url, added=added, cleared=cleared),
            score_blocks(card, repo_name=sess.repo_name, url=url,
                         added=added, cleared=cleared))
        if why:
            # 링크가 없는 이유를 사용자도 봐야 한다 — 채점 결과는 위에 이미 나갔다.
            await say(text=f"⚠️ 리포트 발행에 실패했습니다: {plain(why, 200)}",
                      thread_ts=sess.thread_ts)

    def _report_id(self, sess: QuizSession) -> str:
        import hashlib
        seed = f"quiz:{sess.thread_ts}:{len(sess.questions)}"
        return "quiz-" + hashlib.sha256(seed.encode()).hexdigest()[:10]

    @staticmethod
    def _next_index(sess: QuizSession) -> int | None:
        return next((i for i in range(len(sess.questions)) if i not in sess.answers), None)

    # ── TA 세션 수명 ────────────────────────────────────────────────────────
    def _ta_lock_for(self, thread_ts: str) -> asyncio.Lock:
        """스레드당 lock 하나 (`slack_engine._lock_for`와 같은 패턴).

        전역 lock 하나면 스레드 B의 학습자가 **남의 질문** 때문에 최대
        `TURN_TIMEOUT`(300초)을 기다리고, 그러면서 "앞선 질문에 답하는 중"이라는
        존재하지 않는 질문에 대한 안내를 받는다. 스펙의 동시성 단위는 스레드다.
        """
        return self._ta_locks.setdefault(thread_ts, asyncio.Lock())

    async def _drop(self, sess: QuizSession) -> None:
        """회차를 메모리에서 놓고 열려 있던 TA 세션을 반납한다 — best-effort.

        회차 자체는 trace가 진실이라 `_resume`이 언제든 되살린다. 여기서 반드시
        회수해야 하는 것은 살아 있는 워커 서브프로세스(`archive`)와 registry 행이다 —
        `archive`만이 SDK 클라이언트를 disconnect한다.
        """
        if sess.ta_session_id:
            try:
                await self.orch.adapters[sess.ta_provider].archive(sess.ta_session_id)
            except Exception:
                pass
        if sess.ta_instance_id:
            try:
                self.orch.registry.finish(sess.ta_instance_id)
            except Exception:
                pass
        sess.ta_session_id = sess.ta_provider = sess.ta_instance_id = None
        self.sessions.pop(sess.thread_ts, None)
        lock = self._ta_locks.get(sess.thread_ts)
        if lock is not None and not lock.locked():
            self._ta_locks.pop(sess.thread_ts, None)

    async def _evict_ta(self) -> None:
        """유휴 반납 + 상한 초과 축출 (`slack_brain._evict`와 같은 규칙).

        **답변 중인 회차와 TA 세션이 없는 회차는 건드리지 않는다.** 전자를 말없이
        죽이면 학습자에겐 답이 끊긴 스레드만 남고, 후자는 아직 풀고 있는 회차라
        반납할 자원 자체가 없다.
        """
        now = time.monotonic()
        live = sorted((s for s in self.sessions.values()
                       if s.ta_session_id and not s.ta_busy),
                      key=lambda s: s.ta_touched)
        for sess in list(live):
            if now - sess.ta_touched <= TA_IDLE_TTL:
                break                    # ta_touched 오름차순 — 뒤는 더 최근이다
            await self._drop(sess)
            live.remove(sess)
        # 분모는 `self.sessions`가 아니라 `live`다. 상한이 묶으려는 것은 살아 있는
        # 워커이고, `self.sessions`에는 TA 세션이 없는 회차(진행 중·아무도 안 물어본
        # 스레드)까지 들어 있는데 그 dict는 `_drop` 말고는 줄지 않는다 — 회차 50개를
        # 넘겨 본 엔진에서는 조건이 영구히 참이 되어 매 답변마다 live가 바닥까지
        # 비워지고, 후보가 방금 답한 회차뿐이면 그 회차가 죽는다. slack_brain에서
        # 같은 코드가 옳은 이유는 거기 sessions에는 인터뷰 세션만 들어 있기 때문이다.
        while len(live) > MAX_TA_SESSIONS:
            await self._drop(live.pop(0))

    async def sweep_idle(self) -> None:
        """유휴 TA 세션 반납 — **주기적으로** 불린다 (`sweep_loop`).

        `_evict_ta`와 같은 일을 하지만 부르는 자리가 다르다. 답변 경로에서만 돌면
        "마지막 질문 뒤 아무도 안 묻는" 흔한 경우에 TTL이 무의미해진다.
        """
        await self._evict_ta()
        self.sweep_corpus()

    def sweep_corpus(self) -> list[str]:
        """만료된 회차의 자료를 회수한다 → 지운 디렉토리 이름들 (§10.8).

        **일회성이 전제다.** 주제 학습은 대개 한 번 읽고 끝이라, 안 지우면 최신화되지
        않는 스냅샷만 무한히 쌓인다. 회차 자체가 `ROUND_TTL`에 닫히므로 자료의 수명도
        거기 맞춘다 — 그 뒤 재출제를 시도하면 `verify_citations`가 근거를 폐기하고
        새 문항이 나온다(fail-safe).

        답변 중인 회차는 건드리지 않는다. TA는 이 자료를 근거로 읽고 있다.
        """
        if not self.corpus_root.is_dir():
            return []
        busy = {Path(s.corpus_dir).name for s in self.sessions.values()
                if s.corpus_dir and s.ta_busy}
        removed: list[str] = []
        cutoff = time.time() - ROUND_TTL
        for d in sorted(self.corpus_root.iterdir()):
            if not d.is_dir() or d.name in busy:
                continue
            try:
                if d.stat().st_mtime > cutoff:
                    continue
                shutil.rmtree(d)
            except OSError:
                # 정리 실패가 답변 경로를 죽이면 안 된다. 다음 주기에 다시 만난다.
                continue
            removed.append(d.name)
        return removed

    async def _expire(self, thread_ts: str, say, sess: QuizSession | None = None) -> None:
        """만료된 회차를 닫는다 — 세션을 반납하고, 안내는 **회차당 한 번만**.

        게이트가 없으면 그 스레드의 모든 답글마다 안내가 나간다. tutor 메시지는 전부
        스레드가 그때마다 울리고, owner 검사보다 앞이라 남의 답글에도
        나간다 — "봇과 무관한 대화마다 끼어들지 않는다"는 규칙을 정면으로 어기는 자리다.

        그리고 **거절하면서 워커를 살려 두지 않는다.** "이 회차는 못 쓴다"고 말하는
        바로 여기가 그 회차의 세션을 반납할 마지막 자리다.
        """
        sess = sess if sess is not None else self.sessions.get(thread_ts)
        if sess is not None:
            await self._drop(sess)
        if thread_ts in self._expired:
            return
        if len(self._expired) >= self._max_seen:
            self._expired.clear()
        self._expired.add(thread_ts)
        await say(text=STALE_MSG, thread_ts=thread_ts)

    # ── 재개 ────────────────────────────────────────────────────────────────
    async def _resume(self, thread_ts: str, say) -> QuizSession | None:
        """세션이 사라진 스레드 — trace의 회차 기록에서 되살린다."""
        exec_id = round_id(thread_ts)
        try:
            evs = sorted(self.orch.trace.events(execution_id=exec_id),
                         key=lambda e: e["id"])
        except Exception:
            return None
        issued = [e for e in evs if e["event_type"] == ISSUED_EVENT]
        if not issued:
            return None
        last = issued[-1]
        if time.time() - last["ts"] > ROUND_TTL:
            await self._expire(thread_ts, say)
            return None
        payload = last["payload"]
        questions = from_raw(payload.get("questions") or [])
        if not questions:
            return None
        answers = {}
        for e in evs:
            if e["id"] > last["id"] and e["event_type"] == ANSWER_EVENT:
                p = e["payload"]
                answers[int(p["index"])] = int(p["choice"])
        sess = QuizSession(channel=payload.get("channel", ""), thread_ts=thread_ts,
                           owner=payload.get("owner", ""),
                           repo_name=payload.get("repo_name", ""),
                           questions=questions, answers=answers,
                           corpus_dir=payload.get("corpus_dir") or None,
                           started_at=last["ts"])
        # ROUND_GRADED_EVENT가 있으면 끝난 회차다. 이걸 복원하지 않으면 재시작 뒤에는
        # 채점이 끝난 스레드가 "진행 중"으로 보여 후속 질문이 거절된다.
        #
        # GRADED_EVENT(오답 노트)로 판정하면 안 된다 — 그건 note_id(사용자·repo)
        # 네임스페이스라 여러 회차가 공유한다. "이 회차를 방치하고 같은 repo로 새
        # 회차를 채점"하면 그 GRADED_EVENT가 방치한 회차까지 끝난 것으로 보이게
        # 만들어, 아직 풀지 않은 문항의 정답·해설이 새는 정답 유출이 된다 (실제
        # 재현됨: 리뷰 2026-08-24). evs는 이미 이 회차의 execution_id(QUIZ-*)로
        # 스코프돼 있으므로 여기서 찾는 ROUND_GRADED_EVENT는 이 회차만 가리킨다.
        #
        # 이 변경 이전에 채점된 회차는 ROUND_GRADED_EVENT가 없어 done=False로
        # 복원된다 — fail-closed다. 다시 완주해야 후속 질문을 받을 수 있지만,
        # 그게 안전한 실패 방향이다.
        sess.done = any(e["id"] > last["id"] and e["event_type"] == ROUND_GRADED_EVENT
                        for e in evs)
        self.sessions[thread_ts] = sess
        return sess

    # ── 보조 ────────────────────────────────────────────────────────────────
    def _dedupe(self, body: dict) -> bool:
        event_id = body.get("event_id")
        if not event_id:
            return False
        if event_id in self._seen:
            return True
        if len(self._seen) >= self._max_seen:
            self._seen.clear()
        self._seen.add(event_id)
        return False

    async def _ack(self, event: dict) -> None:
        if self.react and event.get("channel") and event.get("ts"):
            try:
                await self.react(event["channel"], event["ts"])
            except Exception:
                pass

    async def _set_status(self, channel: str, thread_ts, text: str) -> None:
        if self.status and channel and thread_ts:
            try:
                await self.status(channel, thread_ts, text)
            except Exception:
                pass


async def sweep_loop(handler, *, interval: float = SWEEP_INTERVAL) -> None:
    """유휴 TA 세션을 주기적으로 반납한다. 취소될 때까지 돈다.

    **모듈 레벨에 둔다** — bolt 핸들러 클로저 안에 두면 배선이 통째로 사라져도 테스트가
    전부 초록이다 (lessons C1). 엔진은 이걸 task로 띄우기만 한다.

    한 번의 실패가 루프를 죽이면 이후 전부 안 돈다 — 그건 고치기 전보다 나쁘다.
    그래서 삼키되, 여기서 삼키는 것은 **곁다리 청소의 실패**이지 사용자에게 보일
    결과가 아니다.
    """
    while True:
        await asyncio.sleep(interval)
        try:
            await handler.sweep_idle()
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
