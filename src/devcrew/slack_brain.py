"""@brain — 그릴링 인터뷰 봇. Slack 스레드 = BRAIN 세션 1개.

흐름: 채널에서 @brain 멘션(주제, 선택적으로 `repo명:` 접두) → 스레드에서 질문/답변
반복(세션 유지, §11.1 CONTINUE) → 사용자가 "전달" 포함 답글 → 별도 요약 세션이
스키마 강제로 brief 산출 → 채널(스레드 밖)에 🧠→🛠 핸드오프 게시 + crew 실행 트리거.

컨텍스트 경계: crew로 넘어가는 것은 구조화 brief 텍스트뿐 — 인터뷰 대화 이력은
brain 세션에 남고 세션은 핸드오프 후 archive된다.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field

from .repos import RepoRegistryError, split_repo_prefix
from .schema import AgentInstance, Role

_MENTION_RE = re.compile(r"<@[A-Z0-9]+>")
HANDOFF_KEYWORD = "전달"
TURN_TIMEOUT = 300.0


@dataclass
class BrainSession:
    inst: AgentInstance
    session_id: str
    channel: str
    thread_ts: str
    repo_name: str | None
    transcript: list[str] = field(default_factory=list)


def format_brief(brief: dict) -> str:
    lines = [f"*목표*: {brief.get('goal', '')}"]
    if brief.get("decisions"):
        lines.append("*결정*:\n" + "\n".join(f"• {d}" for d in brief["decisions"]))
    if brief.get("constraints"):
        lines.append("*제약*:\n" + "\n".join(f"• {c}" for c in brief["constraints"]))
    if brief.get("acceptance_criteria"):
        lines.append("*수용 기준*:\n" + "\n".join(f"• {a}" for a in brief["acceptance_criteria"]))
    if brief.get("open_questions"):
        lines.append("*열린 질문*:\n" + "\n".join(f"• {q}" for q in brief["open_questions"]))
    return "\n".join(lines)


def brief_to_task(brief: dict, repo_name: str | None) -> str:
    """brief → crew에 넘길 task 텍스트. `repo명:` 접두는 crew가 registry로 해석한다."""
    parts = [brief.get("goal", "")]
    if brief.get("decisions"):
        parts.append("결정사항: " + "; ".join(brief["decisions"]))
    if brief.get("constraints"):
        parts.append("제약: " + "; ".join(brief["constraints"]))
    if brief.get("acceptance_criteria"):
        parts.append("수용 기준: " + "; ".join(brief["acceptance_criteria"]))
    task = ". ".join(p for p in parts if p)
    repo = brief.get("target_repo") or repo_name
    return f"{repo}: {task}" if repo else task


class BrainHandler:
    """멘션→인터뷰 시작, 스레드 답글→세션 지속, '전달'→brief 산출·핸드오프.

    crew_dispatch: async (task: str, channel: str, interview_link: str) — 핸드오프
    게시와 crew 실행을 담당하는 콜백 (bolt 배선은 slack_engine이 소유).
    """

    def __init__(self, orch, cfg, repos: dict, crew_dispatch, *, max_seen: int = 1000,
                 react=None):
        self.react = react            # async (channel, ts) — 수신 확인 리액션 (선택)
        self.orch = orch
        self.cfg = cfg
        self.repos = repos
        self.crew_dispatch = crew_dispatch
        self.sessions: dict[str, BrainSession] = {}
        self._seen: set[str] = set()
        self._max_seen = max_seen
        self._lock = asyncio.Lock()          # 인터뷰 turn 직렬화 (세션당 동시 1 turn)

    async def _ack(self, event: dict) -> None:
        """수신 확인 리액션 — 처리 대기 중임을 요청 메시지에 표기 (best-effort)."""
        if self.react and event.get("channel") and event.get("ts"):
            try:
                await self.react(event["channel"], event["ts"])
            except Exception:
                pass

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

    async def on_mention(self, body: dict, say) -> None:
        """@brain 멘션 — 새 인터뷰 시작."""
        if self._dedupe(body):
            return
        event = body.get("event") or {}
        thread_ts = event.get("thread_ts") or event.get("ts")
        if thread_ts in self.sessions:       # 진행 중 스레드 안에서의 멘션은 답글로 처리
            await self.on_thread_message(body, say, deduped=True)
            return
        topic = _MENTION_RE.sub("", event.get("text") or "").strip()
        if not topic:
            await say(text="⚠️ 주제가 비어 있습니다. `@brain [repo명:] <기능 요청>` 형식으로 시작하세요.",
                      thread_ts=thread_ts)
            return
        try:
            repo_name, topic = split_repo_prefix(topic, self.repos)
        except RepoRegistryError as e:
            await say(text=f"⚠️ {e}", thread_ts=thread_ts)
            return
        await self._ack(event)

        async with self._lock:
            tier = self.cfg.role_defaults[Role.BRAIN].tier
            worktree = str(self.repos[repo_name]) if repo_name else None
            inst = await self.orch.spawn(Role.BRAIN, tier,
                                         execution_id=f"BRAIN-{thread_ts}",
                                         node_id="interview", task_scope=topic,
                                         worktree=worktree)
            sid = await self.orch.start_worker(
                inst, f"인터뷰 주제: {topic}", conversational=True)
            adapter = self.orch.adapters[inst.provider]
            out = await asyncio.wait_for(
                adapter.send(sid, "인터뷰를 시작해라. 첫 질문 하나를 권장안과 함께 던져라."),
                timeout=TURN_TIMEOUT)
        sess = BrainSession(inst=inst, session_id=sid, channel=event.get("channel", ""),
                            thread_ts=thread_ts, repo_name=repo_name)
        sess.transcript.append(f"[사용자] {topic}")
        sess.transcript.append(f"[brain] {out.text}")
        self.sessions[thread_ts] = sess
        await say(text=out.text + "\n\n_(이 스레드에 답글로 대화를 이어가세요 — "
                                  f"끝나면 '{HANDOFF_KEYWORD}'라고 하면 crew에 넘깁니다)_",
                  thread_ts=thread_ts)

    async def on_thread_message(self, body: dict, say, *, deduped: bool = False) -> None:
        """진행 중 인터뷰 스레드의 답글 — 세션 지속 또는 핸드오프."""
        if not deduped and self._dedupe(body):
            return
        event = body.get("event") or {}
        if event.get("bot_id"):              # 봇 메시지(자기 자신·crew) 무시 — 루프 차단
            return
        thread_ts = event.get("thread_ts")
        sess = self.sessions.get(thread_ts)
        if sess is None:
            return
        text = _MENTION_RE.sub("", event.get("text") or "").strip()
        if not text:
            return
        await self._ack(event)
        sess.transcript.append(f"[사용자] {text}")

        if HANDOFF_KEYWORD in text:
            await self._finalize(sess, say)
            return

        try:
            async with self._lock:
                adapter = self.orch.adapters[sess.inst.provider]
                out = await asyncio.wait_for(adapter.send(sess.session_id, text),
                                             timeout=TURN_TIMEOUT)
        except Exception as e:
            await say(text=f"💥 인터뷰 turn 실패: {type(e).__name__}: {e}",
                      thread_ts=thread_ts)
            return
        sess.transcript.append(f"[brain] {out.text}")
        await say(text=out.text, thread_ts=thread_ts)

    async def _finalize(self, sess: BrainSession, say) -> None:
        """대화 transcript → 스키마 강제 brief → 채널 핸드오프 → crew 실행."""
        await say(text="📝 인터뷰를 정리해 crew에 넘길 brief를 만듭니다…",
                  thread_ts=sess.thread_ts)
        transcript = "\n".join(sess.transcript)
        try:
            async with self._lock:
                tier = self.cfg.role_defaults[Role.BRAIN].tier
                summ = await self.orch.spawn(Role.BRAIN, tier,
                                             execution_id=f"BRAIN-{sess.thread_ts}",
                                             node_id="brief", task_scope="brief 산출")
                sid = await self.orch.start_worker(
                    summ, "다음 인터뷰 대화록을 읽고 crew에 전달할 최종 brief를 만들어라.\n\n"
                          + transcript)
                adapter = self.orch.adapters[summ.provider]
                out = await asyncio.wait_for(
                    adapter.send(sid, "이제 최종 brief를 스키마대로 제출해라."),
                    timeout=TURN_TIMEOUT)
        except Exception as e:
            await say(text=f"💥 brief 산출 실패: {type(e).__name__}: {e}",
                      thread_ts=sess.thread_ts)
            return

        brief = out.structured
        if not isinstance(brief, dict) or brief.get("status") != "PASS":
            reason = (brief or {}).get("summary", "구조화 출력 없음") if isinstance(brief, dict) else "구조화 출력 없음"
            open_qs = "\n".join(f"• {q}" for q in (brief or {}).get("open_questions", [])) \
                if isinstance(brief, dict) else ""
            await say(text=f"🙋 아직 전달할 수준이 아닙니다: {reason}\n{open_qs}\n"
                           "_(스레드에서 계속 결정을 닫은 뒤 다시 '전달'하세요)_",
                      thread_ts=sess.thread_ts)
            return

        await say(text="✅ brief 확정:\n" + format_brief(brief), thread_ts=sess.thread_ts)
        task = brief_to_task(brief, sess.repo_name)
        link = sess.thread_ts
        await self.crew_dispatch(task, sess.channel, link, format_brief(brief))
        # 핸드오프 완료 — 세션 정리 (대화 이력은 crew로 넘어가지 않는다)
        try:
            adapter = self.orch.adapters[sess.inst.provider]
            await adapter.archive(sess.session_id)
        except Exception:
            pass
        self.orch.registry.finish(sess.inst.instance_id)
        del self.sessions[sess.thread_ts]
