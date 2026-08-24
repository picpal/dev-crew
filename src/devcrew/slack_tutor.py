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
import time
from dataclasses import dataclass, field

from .quiz import (ANSWER_EVENT, ISSUED_EVENT, QUESTION_EVENT,
                   REPORT_FAILED_EVENT, ROUND_GRADED_EVENT,
                   TA_ANSWER_EVENT, NoteUnavailable, Question, Scorecard,
                   from_raw, grade, note_id, open_misses, record_scorecard, to_raw)
from .report.quiz_report import render_quiz_report, render_ta_answer
from .report.uploader import publish_report
from .repos import RepoRegistryError, format_repo_names, split_repo_prefix
from .slack_brain import to_mrkdwn
from .tutor import issue_quiz
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
ANSWER_RE = re.compile(r"^(\d+):([0-3])$")
REGRADE_VALUE = "__REGRADE__"   # 채점 실패 후 다시 채점하는 버튼의 sentinel
LETTERS = "ABCDEFGH"
BAR_FULL, BAR_EMPTY = "▰", "▱"
TYPE_HINT = {"CORRECT": ("✅", "옳은 것"), "INCORRECT": ("⛔", "틀린 것")}
AREA_LINES = 12          # 채점 결과에 펼치는 영역 줄 수 상한
NEED_REPO = ("⚠️ 대상 repo를 지정해 주세요 — `@tutor <repo명>: ` 형식입니다.\n"
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
                 publish=publish_report, max_seen: int = 1000):
        self.orch = orch
        self.cfg = cfg
        self.repos = repos
        self.react = react
        self.status = status
        self.publish = publish
        self.sessions: dict[str, QuizSession] = {}
        self.last_questions: list[Question] = []   # 최근 회차 (운영·테스트 편의)
        self._seen: set[str] = set()
        self._max_seen = max_seen
        self._opening: set[str] = set()
        self._lock = asyncio.Lock()
        self._ta_locks: dict[str, asyncio.Lock] = {}
        self._expired: set[str] = set()      # 만료 안내를 이미 보낸 스레드

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
        if not repo_name:
            await say(text=NEED_REPO.format(repos=format_repo_names(self.repos)),
                      thread_ts=thread_ts)
            return
        if thread_ts in self.sessions or thread_ts in self._opening:
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
        repo_path = str(self.repos.get(sess.repo_name) or "")
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
        if len(res.text) <= SLACK_LIMIT:
            await say(text=rich(res.text), thread_ts=thread_ts)
            return
        try:
            html = render_ta_answer(question=question, answer=res.text,
                                    citations=res.citations, repo=sess.repo_name)
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
        await say(text=f"{rich(head)}\n\n📄 전체 답변: {url}", thread_ts=thread_ts)

    @staticmethod
    def _ta_report_id(thread_ts: str, question: str) -> str:
        import hashlib
        seed = f"ta:{thread_ts}:{question}"
        return "ta-" + hashlib.sha256(seed.encode()).hexdigest()[:10]

    # ── 회차 ────────────────────────────────────────────────────────────────
    async def _start(self, thread_ts, channel, user, repo_name, say) -> None:
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
                                       repo_path=str(self.repos[repo_name]),
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
                                            "channel": channel})
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
                           repo_name=repo_name, questions=res.questions)
        self.sessions[thread_ts] = sess
        self.last_questions = res.questions
        await self._post_question(sess, 0, say)

    @staticmethod
    async def _say_blocks(say, thread_ts, text: str, blocks: list[dict]) -> None:
        try:
            await say(text=text, thread_ts=thread_ts, blocks=blocks)
        except TypeError:
            await say(text=text, thread_ts=thread_ts)

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
