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
from .report.brain_report import render_brief, render_reply, report_id
from .report.uploader import publish_report
from .schema import AgentInstance, Role

_MENTION_RE = re.compile(r"<@[A-Z0-9]+>")
_OPTION_RE = re.compile(r"^([A-Z])\)\s+(.+)$")
ANSWER_MARKER = "\U0001F4E9 선택 답변:"     # 리포트 폼(worker)이 게시하는 답변 접두
HANDOFF_KEYWORD = "전달"
TURN_TIMEOUT = 300.0
REPORT_THRESHOLD = 500      # 이보다 긴 응답은 HTML 리포트 링크로 제공


@dataclass
class BrainSession:
    inst: AgentInstance
    session_id: str
    channel: str
    thread_ts: str
    repo_name: str | None
    topic: str = ""
    transcript: list[str] = field(default_factory=list)


def to_mrkdwn(text: str) -> str:
    """표준 마크다운 → Slack mrkdwn 방어적 변환 (모델이 규칙을 어겨도 가독성 유지).

    HTML 리포트는 원문 마크다운을 그대로 쓰므로 Slack 발신 직전에만 적용한다."""
    out = []
    for line in text.splitlines():
        m = re.match(r"^\s*#{1,6}\s+(.+)$", line)
        if m:
            out.append(f"*{m.group(1).strip()}*")
            continue
        out.append(line)
    t = "\n".join(out)
    t = re.sub(r"\*\*(.+?)\*\*", r"*\1*", t)          # **bold** → *bold*
    t = re.sub(r"(?m)^(\s*)\* ", r"\1- ", t)             # "* " 불릿 → "- "
    return t


def parse_options(text: str) -> list[str]:
    """`A) 내용` 형식 줄들을 선택지로 추출 (2개 이상일 때만 유효)."""
    opts = [m.group(0).strip() for line in text.splitlines()
            if (m := _OPTION_RE.match(line.strip()))]
    return opts if len(opts) >= 2 else []


def question_blocks(text: str, options: list[str], *, decided: int = 0) -> list[dict]:
    """질문 Block Kit — header(질문) / 맥락 / divider / 선택지 상세 / 짧은 버튼 / 진행.

    시각적 위계: 질문 한 줄은 header로 크게, 선택지 전문은 본문 리스트로,
    버튼은 'A 선택'처럼 짧게 (75자 제한으로 긴 선택지가 잘리는 문제 방지).
    '(권장)' 선택지는 primary 스타일."""
    lines = [l for l in text.splitlines()]
    non_empty = [l.strip() for l in lines if l.strip()]
    # 질문 헤더: 물음표로 끝나는 첫 줄, 없으면 첫 줄
    head = next((l for l in non_empty if l.endswith("?")), non_empty[0] if non_empty else "질문")
    head = re.sub(r"[*_`]", "", head)[:150]
    option_set = set(options)
    context_lines = [l for l in lines
                     if l.strip() and l.strip() != head and l.strip() not in option_set
                     and not _OPTION_RE.match(l.strip())]
    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": head}}]
    if context_lines:
        blocks.append({"type": "section",
                       "text": {"type": "mrkdwn", "text": "\n".join(context_lines)[:2900]}})
    blocks.append({"type": "divider"})
    detail = "\n".join(f"*{o[:2]}* {o[3:].strip()}" for o in options)
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": detail[:2900]}})
    buttons = []
    for opt in options[:10]:                      # actions block 버튼 한도
        letter = opt[0]
        btn = {"type": "button", "action_id": f"brain_answer_{letter}",
               "text": {"type": "plain_text",
                        "text": f"{letter} 선택" + (" ★" if "(권장)" in opt else "")},
               "value": opt[:2000]}
        if "(권장)" in opt:
            btn["style"] = "primary"
        buttons.append(btn)
    blocks.append({"type": "actions", "block_id": "brain_answers", "elements": buttons})
    progress = f" · 닫힌 결정 {decided}개" if decided else ""
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
        "text": f"버튼 선택 또는 답글로 직접 입력 · 끝나면 '전달'{progress}"}]})
    return blocks


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
                 react=None, status=None, publish=publish_report):
        self.react = react            # async (channel, ts) — 수신 확인 리액션 (선택)
        self.status = status          # async (channel, thread_ts, text) — AI 앱 상태 (선택)
        self.publish = publish        # (task_id, html) -> url|None — 리포트 업로드
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

    async def _say_reply(self, say, sess: BrainSession, text: str, thread_ts,
                         *, first: bool = False) -> None:
        """brain 응답 발신 — 선택형 질문이면 스레드 내 버튼(Block Kit), 아니면 텍스트
        (긴 응답은 리포트 링크)."""
        options = parse_options(text)
        guide = ("\n\n_(버튼 선택 또는 답글로 대화 — "
                 f"끝나면 '{HANDOFF_KEYWORD}'라고 하면 crew에 넘깁니다)_" if first else "")
        slack_text = to_mrkdwn(text)
        if options:
            decided = max(0, sum(1 for t in sess.transcript if t.startswith("[사용자]")) - 1)
            try:
                await say(text=slack_text[:2900] + guide, thread_ts=thread_ts,
                          blocks=question_blocks(slack_text, options, decided=decided))
                return
            except TypeError:
                pass                              # say가 blocks 미지원(테스트 대역 등)
        body = await self._with_report(sess, text)
        await say(text=to_mrkdwn(body) + guide, thread_ts=thread_ts)

    async def on_answer(self, *, thread_ts: str, value: str, say, strip=None) -> None:
        """스레드 내 버튼 클릭 → 선택지를 사용자 답변으로 처리."""
        sess = self.sessions.get(thread_ts)
        if sess is None:
            await say(text="⚠️ 이 인터뷰 세션은 종료됐습니다. 새로 @brain 멘션으로 시작하세요.",
                      thread_ts=thread_ts)
            return
        if strip:
            try:
                await strip()                     # 원 메시지 버튼 제거 + 선택 표기
            except Exception:
                pass
        await self._process_answer(sess, value, say)

    async def _process_answer(self, sess: BrainSession, text: str, say) -> None:
        sess.transcript.append(f"[사용자] {text}")
        if HANDOFF_KEYWORD in text:
            await self._finalize(sess, say)
            return
        await self._set_status(sess.channel, sess.thread_ts, "생각 중…")
        try:
            async with self._lock:
                adapter = self.orch.adapters[sess.inst.provider]
                out = await asyncio.wait_for(adapter.send(sess.session_id, text),
                                             timeout=TURN_TIMEOUT)
        except Exception as e:
            await say(text=f"💥 인터뷰 turn 실패: {type(e).__name__}: {e}",
                      thread_ts=sess.thread_ts)
            return
        sess.transcript.append(f"[brain] {out.text}")
        await self._say_reply(say, sess, out.text, sess.thread_ts)

    async def _with_report(self, sess: BrainSession, full_text: str) -> str:
        """긴 응답은 HTML 리포트로 업로드하고 요약+링크를 반환. 실패·미설정 시 원문 유지."""
        if len(full_text) < REPORT_THRESHOLD:
            return full_text
        try:
            html = render_reply(topic=sess.topic or "인터뷰", mode_hint="BRAIN 인터뷰",
                                repo=sess.repo_name, text=full_text)
            rid = report_id(f"{sess.thread_ts}:{len(sess.transcript)}")
            url = await asyncio.to_thread(self.publish, rid, html)
        except Exception:
            return full_text
        if not url:
            return full_text
        head = full_text.strip().split("\n\n")[0][:300]
        return f"{head}\n\n📄 전체 응답: {url}"

    async def _set_status(self, channel: str, thread_ts, text: str) -> None:
        """AI 앱 상태 인디케이터 — 스레드 밑 '생각 중…' 표기 (best-effort).
        봇이 답글을 게시하면 Slack이 자동으로 지운다."""
        if self.status and channel and thread_ts:
            try:
                await self.status(channel, thread_ts, text)
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
        await self._set_status(event.get("channel", ""), thread_ts, "생각 중…")

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
                            thread_ts=thread_ts, repo_name=repo_name, topic=topic)
        sess.transcript.append(f"[사용자] {topic}")
        sess.transcript.append(f"[brain] {out.text}")
        self.sessions[thread_ts] = sess
        await self._say_reply(say, sess, out.text, thread_ts, first=True)

    async def on_thread_message(self, body: dict, say, *, deduped: bool = False) -> None:
        """진행 중 인터뷰 스레드의 답글 — 세션 지속 또는 핸드오프."""
        if not deduped and self._dedupe(body):
            return
        event = body.get("event") or {}
        text = _MENTION_RE.sub("", event.get("text") or "").strip()
        if event.get("bot_id"):
            # 봇 메시지는 무시하되, 리포트 폼(worker)이 게시한 선택 답변 마커는 수용
            if not text.startswith(ANSWER_MARKER):
                return
            text = text[len(ANSWER_MARKER):].strip()
        thread_ts = event.get("thread_ts")
        sess = self.sessions.get(thread_ts)
        if sess is None or not text:
            return
        await self._ack(event)
        await self._process_answer(sess, text, say)

    async def _finalize(self, sess: BrainSession, say) -> None:
        """대화 transcript → 스키마 강제 brief → 채널 핸드오프 → crew 실행."""
        await self._set_status(sess.channel, sess.thread_ts, "brief 정리 중…")
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

        brief_url = None
        try:
            html = render_brief(brief=brief, repo=sess.repo_name)
            brief_url = await asyncio.to_thread(
                self.publish, report_id(f"brief:{sess.thread_ts}"), html)
        except Exception:
            brief_url = None
        confirm = "✅ brief 확정:\n" + format_brief(brief)
        if brief_url:
            confirm += f"\n\n📄 리포트: {brief_url}"
        await say(text=confirm, thread_ts=sess.thread_ts)
        task = brief_to_task(brief, sess.repo_name)
        link = sess.thread_ts
        handoff_text = format_brief(brief)
        if brief_url:
            handoff_text += f"\n📄 리포트: {brief_url}"
        await self.crew_dispatch(task, sess.channel, link, handoff_text)
        # 핸드오프 완료 — 세션 정리 (대화 이력은 crew로 넘어가지 않는다)
        try:
            adapter = self.orch.adapters[sess.inst.provider]
            await adapter.archive(sess.session_id)
        except Exception:
            pass
        self.orch.registry.finish(sess.inst.instance_id)
        del self.sessions[sess.thread_ts]
