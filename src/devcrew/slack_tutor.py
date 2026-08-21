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

from .quiz import (ANSWER_EVENT, ISSUED_EVENT, Question, from_raw, grade, note_id,
                   open_misses, record_scorecard, to_raw)
from .report.quiz_report import render_quiz_report
from .report.uploader import publish_report
from .repos import RepoRegistryError, split_repo_prefix
from .tutor import issue_quiz

_MENTION_RE = re.compile(r"<@[A-Z0-9]+>")
ROUND_TTL = 24 * 3600.0     # 미완 회차의 수명 — 하루가 지나면 코드도 기억도 달라진다
ANSWER_RE = re.compile(r"^(\d+):([0-3])$")
NEED_REPO = ("⚠️ 대상 repo를 지정해 주세요 — `@tutor <repo명>: ` 형식입니다.\n"
             "등록된 repo: {repos}")
STALE_MSG = ("⚠️ 이 회차는 하루가 지나 이어서 풀 수 없습니다. "
             "`@tutor <repo명>:` 로 다시 시작해 주세요.")
NOT_OWNER = "⚠️ 이 회차를 시작한 사람만 답할 수 있습니다."


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


def question_blocks(q: Question, idx: int, total: int) -> list[dict]:
    """문항 하나 — 진행 표시 / 지문 / 보기 4개 버튼. 정오는 표시하지 않는다."""
    kind = "옳은 것" if q.type == "CORRECT" else "틀린 것"
    detail = "\n".join(f"*{chr(65 + i)})* {o}" for i, o in enumerate(q.options))
    return [
        {"type": "context", "elements": [{"type": "mrkdwn",
                                          "text": f"*{idx + 1} / {total}* · {q.area}"}]},
        {"type": "header", "text": {"type": "plain_text", "text": q.stem[:150]}},
        {"type": "section", "text": {"type": "mrkdwn", "text": detail[:2900]}},
        {"type": "actions", "block_id": "tutor_answers", "elements": [
            {"type": "button", "action_id": f"tutor_answer_{idx}_{i}",
             "text": {"type": "plain_text", "text": chr(65 + i)},
             "value": f"{idx}:{i}"} for i in range(len(q.options))]},
        {"type": "context", "elements": [{"type": "mrkdwn",
                                          "text": f"_{kind}을 고르세요 · 채점은 마지막에 한 번에 합니다_"}]},
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
            await say(text=NEED_REPO.format(repos=", ".join(f"`{r}`" for r in self.repos)),
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
        m = ANSWER_RE.match(value or "")
        if not m:
            return
        idx, choice = int(m.group(1)), int(m.group(2))
        sess = self.sessions.get(thread_ts) or await self._resume(thread_ts, say)
        if sess is None:
            return
        if sess.owner and user != sess.owner:
            # 대화는 누구나 볼 수 있지만 답은 회차 주인의 것이다 — 남이 채점을 흔들면 안 된다
            await say(text=NOT_OWNER, thread_ts=thread_ts)
            return
        if sess.done or not 0 <= idx < len(sess.questions):
            return
        if strip:
            try:
                await strip()
            except Exception:
                pass
        sess.answers[idx] = choice
        self.orch.trace.append(ANSWER_EVENT, task_id=round_id(thread_ts),
                               execution_id=round_id(thread_ts),
                               payload={"index": idx, "choice": choice})
        nxt = self._next_index(sess)
        if nxt is None:
            await self._finish(sess, say)
            return
        await self._post_question(sess, nxt, say)

    # ── 회차 ────────────────────────────────────────────────────────────────
    async def _start(self, thread_ts, channel, user, repo_name, say) -> None:
        note = note_id(user, repo_name)
        misses = open_misses(self.orch.trace, note)
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
            detail = ("\n" + "\n".join(f"• {n}" for n in res.notes)) if res.notes else ""
            await say(text=f"🙋 근거 있는 문항을 만들지 못했습니다.{detail}",
                      thread_ts=thread_ts)
            return
        total = len(res.questions)
        head = f"🎓 `{repo_name}` 학습 회차 — 총 {total}문항"
        if res.shortfall:
            # 숫자를 채우려 지어내지 않는다. 왜 적은지 사실대로 말한다 (#19 D6)
            head += f"\n⚠️ 근거를 찾지 못해 {total}문항만 출제했습니다."
        if misses:
            head += f"\n📌 지난 오답 {len(misses)}건을 노트에서 보고 있습니다."
        await say(text=head, thread_ts=thread_ts)
        self.orch.trace.append(ISSUED_EVENT, task_id=round_id(thread_ts),
                               execution_id=round_id(thread_ts),
                               payload={"questions": [to_raw(q) for q in res.questions],
                                        "owner": user, "repo_name": repo_name,
                                        "channel": channel})
        sess = QuizSession(channel=channel, thread_ts=thread_ts, owner=user,
                           repo_name=repo_name, questions=res.questions)
        self.sessions[thread_ts] = sess
        self.last_questions = res.questions
        await self._post_question(sess, 0, say)

    async def _post_question(self, sess: QuizSession, idx: int, say) -> None:
        q = sess.questions[idx]
        total = len(sess.questions)
        text = f"*{idx + 1} / {total}* · {q.area}\n{q.stem}"
        try:
            await say(text=text, thread_ts=sess.thread_ts,
                      blocks=question_blocks(q, idx, total))
        except TypeError:
            await say(text=text, thread_ts=sess.thread_ts)

    async def _finish(self, sess: QuizSession, say) -> None:
        sess.done = True
        note = note_id(sess.owner, sess.repo_name)
        prior = {m["q_key"] for m in open_misses(self.orch.trace, note)}
        card = grade(sess.questions, sess.answers)
        added, cleared = record_scorecard(self.orch.trace, note, card, prior_keys=prior)
        url = None
        try:
            html = render_quiz_report(card, repo=sess.repo_name, added=added,
                                      cleared=cleared)
            url = await asyncio.to_thread(self.publish, self._report_id(sess), html)
        except Exception:
            url = None
        lines = [f"✅ 채점 완료 — *{card.correct} / {card.total}*",
                 " · ".join(f"{a} {ok}/{n}" for a, (ok, n) in card.by_area.items())]
        if added or cleared:
            lines.append(f"오답 노트: +{added} / 해소 {cleared}")
        lines.append(f"📄 리포트: {url}" if url
                     else "_(리포트 발행에 실패했습니다 — 점수만 전달합니다)_")
        await say(text="\n".join(lines), thread_ts=sess.thread_ts)

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
                           questions=questions, answers=answers)
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
