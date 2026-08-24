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

from .quiz import (ANSWER_EVENT, GRADED_EVENT, ISSUED_EVENT, QUESTION_EVENT,
                   TA_ANSWER_EVENT, NoteUnavailable, Question, Scorecard,
                   from_raw, grade, note_id, open_misses, record_scorecard, to_raw)
from .report.quiz_report import render_quiz_report
from .report.uploader import publish_report
from .repos import RepoRegistryError, format_repo_names, split_repo_prefix
from .slack_brain import to_mrkdwn
from .tutor import issue_quiz
from .tutor_ta import TutorTAError, ask, round_context

_MENTION_RE = re.compile(r"<@[A-Z0-9]+>")
_WS_RE = re.compile(r"\s+")
_MD_MARKS_RE = re.compile(r"[*_`~#]")
ROUND_TTL = 24 * 3600.0     # 미완 회차의 수명 — 하루가 지나면 코드도 기억도 달라진다
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
        self._ta_lock = asyncio.Lock()

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
        if sess.owner and user != sess.owner:
            await say(text=NOT_OWNER, thread_ts=thread_ts)
            return
        if time.time() - sess.started_at > ROUND_TTL:
            # 하루가 지나면 코드도 기억도 달라진다. 그때의 근거로 답하는 것이 오히려
            # 틀린 설명이 된다. 메모리 세션이 살아 있어도 같은 규칙을 쓴다.
            await say(text=STALE_MSG, thread_ts=thread_ts)
            return
        if self._ta_lock.locked():
            await say(text=TA_BUSY, thread_ts=thread_ts)
        await self._set_status(channel or sess.channel, thread_ts, "답변 준비 중…")
        repo_path = str(self.repos.get(sess.repo_name) or "")
        try:
            async with self._ta_lock:
                # context는 세션을 새로 열 때만 쓰이지만 항상 계산해 넘긴다 — ask()는
                # 이 adapter 인스턴스가 session_id를 모르게 됐을 때(cross-process 재시작
                # 등) context가 있어야만 새 세션으로 자연 복구한다. 재사용 경로에서
                # None을 넘기면 그 복구 코드가 죽은 채로 남고, 세션을 잃은 학습자는
                # "새 질문으로 다시 시작해 주세요"만 영원히 본다. round_context 조립은
                # 순수 문자열 작업이라 매번 계산해도 비용은 없다.
                res = await ask(
                    self.orch, self.cfg, exec_id=round_id(thread_ts),
                    repo_path=repo_path, question=question,
                    session_id=sess.ta_session_id, provider=sess.ta_provider,
                    context=round_context(sess.questions, sess.answers,
                                          repo_name=sess.repo_name))
        except TutorTAError as e:
            await say(text=TA_FAIL.format(reason=e), thread_ts=thread_ts)
            return
        sess.ta_session_id, sess.ta_provider = res.session_id, res.provider
        # **기록이 먼저다.** 기록에 실패했는데 답이 나가면, 사용자는 답을 봤는데 우리는
        # 무엇을 답했는지 모르는 상태가 된다 (lessons C7).
        try:
            self.orch.trace.append(QUESTION_EVENT, task_id=round_id(thread_ts),
                                   execution_id=round_id(thread_ts),
                                   payload={"user": user, "question": question})
            self.orch.trace.append(
                TA_ANSWER_EVENT, task_id=round_id(thread_ts),
                execution_id=round_id(thread_ts),
                payload={"answer": res.text, "dropped": res.dropped,
                         "citations": [{"path": c.path, "start_line": c.start_line,
                                        "end_line": c.end_line} for c in res.citations]})
        except Exception:
            pass          # 기록 실패가 답변을 막지는 않는다 — 답은 이미 만들어졌다
        await say(text=res.text, thread_ts=thread_ts)

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
        try:
            async with self._lock:
                res = await issue_quiz(self.orch, self.cfg, repo_name=repo_name,
                                       repo_path=str(self.repos[repo_name]),
                                       exec_id=round_id(thread_ts), misses=misses)
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
        url = None
        try:
            html = render_quiz_report(card, repo=sess.repo_name, added=added,
                                      cleared=cleared)
            url = await asyncio.to_thread(self.publish, self._report_id(sess), html)
        except Exception:
            url = None
        await self._say_blocks(
            say, sess.thread_ts,
            score_text(card, url=url, added=added, cleared=cleared),
            score_blocks(card, repo_name=sess.repo_name, url=url,
                         added=added, cleared=cleared))

    def _report_id(self, sess: QuizSession) -> str:
        import hashlib
        seed = f"quiz:{sess.thread_ts}:{len(sess.questions)}"
        return "quiz-" + hashlib.sha256(seed.encode()).hexdigest()[:10]

    @staticmethod
    def _next_index(sess: QuizSession) -> int | None:
        return next((i for i in range(len(sess.questions)) if i not in sess.answers), None)

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
            await say(text=STALE_MSG, thread_ts=thread_ts)
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
        # 채점 이벤트가 있으면 끝난 회차다. 이걸 복원하지 않으면 재시작 뒤에는
        # 채점이 끝난 스레드가 "진행 중"으로 보여 후속 질문이 거절된다.
        # GRADED_EVENT는 이 회차의 execution_id(QUIZ-*)가 아니라 오답 노트의
        # execution_id(note_id: 사용자·repo 단위)에 쌓인다 — record_scorecard가
        # 거기에 적기 때문이다. id는 이벤트 테이블 전체에서 단조 증가하므로,
        # 네임스페이스가 달라도 "이 회차가 출제된 뒤에 채점 이벤트가 있었는가"는
        # id 비교로 그대로 판정할 수 있다.
        try:
            graded = self.orch.trace.events(
                execution_id=note_id(sess.owner, sess.repo_name))
        except Exception:
            graded = []
        sess.done = any(e["id"] > last["id"] and e["event_type"] == GRADED_EVENT
                        for e in graded)
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
