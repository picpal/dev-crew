"""Slack → WorkflowEngine 브리지 (§12.1 축약) — Socket Mode로 mention을 받아
DEFAULT_TEMPLATE 워크플로를 실행하고 스레드로 결과를 회신한다.

실행: `uv run python -m devcrew.slack_engine`
필수 env: SLACK_BOT_TOKEN(xoxb), SLACK_APP_TOKEN(xapp, connections:write)
선택(브레인): BRAIN_BOT_TOKEN, BRAIN_APP_TOKEN — 있으면 @brain 인터뷰 앱도 함께 기동
선택 env:
  DEVCREW_TARGET_REPO   실행 대상 git repo 경로 (registry 접두 없을 때의 기본값)
  DEVCREW_RUNTIME_DIR   trace.db/harness.db 위치 (기본 .devcrew-runtime/)
  DEVCREW_ORCH_TIER     ORCHESTRATOR 결정 세션 tier (기본 CHEAP — 비용 원칙)
  DEVCREW_TASK_TIMEOUT  요청당 타임아웃 초 (기본 600)

정책: `repo명: 작업` 접두로 config/repos.yaml registry의 repo를 지정한다. 등록
repo 작업은 execution별 git worktree(브랜치 wt/slack-N)에서 격리 실행되고 원본
working copy는 건드리지 않는다(§8.2). 같은 repo는 직렬, 다른 repo는 병렬. 접두가
없으면 toy repo(또는 DEVCREW_TARGET_REPO). Slack 재전송은 event_id로 중복
제거(§12.3), 엔진 예외·타임아웃은 스레드에 오류로 회신한다.
"""
from __future__ import annotations

import asyncio
import dataclasses
import itertools
import os
import re
import subprocess
import tempfile
from pathlib import Path

from .repos import RepoRegistryError, load_repos, split_repo_prefix
from .worktree import WorktreeManager

_MENTION_RE = re.compile(r"<@[A-Z0-9]+>")


def make_toy_repo() -> Path:
    """대상 repo 미지정 시 요청마다 만드는 격리 실습용 repo."""
    repo = Path(tempfile.mkdtemp(prefix="slack-devcrew-"))
    (repo / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=devcrew@local", "-c", "user.name=devcrew",
                    "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


def format_result(execution_id: str, result, repo: str) -> str:
    """ExecutionResult → Slack 회신 텍스트."""
    icon = {"COMPLETED": "✅", "NEEDS_HUMAN": "🙋", "ABORTED": "❌"}.get(result.status, "❓")
    path = " → ".join(f"{h['node_id']}:{h['transition']}" for h in result.node_history)
    return (f"{icon} {execution_id} {result.status}\n"
            f"경로: {path or '(없음)'}\n"
            f"결정 {result.decisions}회 · 토큰 {result.total_tokens:,}\n"
            f"작업 공간: {repo}")


class EngineRunner:
    """엔진 배선 1회 구성 + 요청 직렬 실행. Slack을 모른다 — task 문자열만 받는다."""

    def __init__(self, runtime_dir: str | Path | None = None):
        from .adapters.claude_code import ClaudeCodeAdapter
        from .adapters.codex import CodexAdapter
        from .config import load as load_config
        from .harness_mcp import build_harness_mcp
        from .orchestrator import Orchestrator
        from .schema import Provider, Role
        from .store.registry import SessionRegistry
        from .store.trace import TraceStore

        rt = Path(runtime_dir or os.environ.get("DEVCREW_RUNTIME_DIR", ".devcrew-runtime"))
        rt.mkdir(parents=True, exist_ok=True)
        self.trace = TraceStore(rt / "trace.db")
        self.registry = SessionRegistry(rt / "harness.db")
        self.orch = Orchestrator(self.trace, self.registry, {
            Provider.CLAUDE_CODE: ClaudeCodeAdapter(self.trace, self.registry),
            Provider.CODEX: CodexAdapter(self.trace, self.registry),
        })
        cfg = load_config()
        orch_tier = os.environ.get("DEVCREW_ORCH_TIER", "CHEAP")
        role_defaults = dict(cfg.role_defaults)
        role_defaults[Role.ORCHESTRATOR] = dataclasses.replace(
            role_defaults[Role.ORCHESTRATOR], tier=orch_tier)
        self.cfg = dataclasses.replace(cfg, role_defaults=role_defaults)
        self.mcp = {"harness": build_harness_mcp(self.trace)}
        self.timeout = float(os.environ.get("DEVCREW_TASK_TIMEOUT", "600"))
        self.repos = load_repos()
        self._locks: dict[str, asyncio.Lock] = {}
        self._seq = itertools.count(1)

    def _lock_for(self, key: str) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    @property
    def busy(self) -> bool:
        return any(l.locked() for l in self._locks.values())

    async def run(self, task: str) -> tuple[str, object, str]:
        """task 1건 실행 → (execution_id, ExecutionResult, repo 경로)."""
        from .decision import make_llm_decide
        from .engine import WorkflowEngine
        from .workflow import DEFAULT_TEMPLATE

        repo_name, task = split_repo_prefix(task, self.repos)   # 오타는 여기서 fail-fast
        # 같은 repo는 직렬(worktree 브랜치 경합·리뷰 혼선 방지), 다른 repo·toy는 병렬
        async with self._lock_for(repo_name or "_toy"):
            n = next(self._seq)
            execution_id = f"SLACK-{n}"
            if repo_name:
                wt = WorktreeManager(self.repos[repo_name]).create(f"slack-{n}")
                workspace, where = str(wt), f"{repo_name} · 브랜치 wt/slack-{n}\n{wt}"
            else:
                workspace = os.environ.get("DEVCREW_TARGET_REPO") or str(make_toy_repo())
                where = workspace
            decide = make_llm_decide(self.orch, self.cfg, mcp_servers=self.mcp)
            engine = WorkflowEngine(self.orch, self.cfg, decide_fn=decide)
            result = await asyncio.wait_for(
                engine.run(execution_id=execution_id, task=task, worktree=workspace),
                timeout=self.timeout)
            return execution_id, result, where


class MentionHandler:
    """app_mention 이벤트 처리 — bolt와 분리된 순수 로직 (테스트 대상)."""

    def __init__(self, runner: EngineRunner, *, max_seen: int = 1000):
        self.runner = runner
        self._seen: set[str] = set()
        self._max_seen = max_seen

    async def __call__(self, body: dict, say) -> None:
        event_id = body.get("event_id")
        if event_id:                                   # §12.3 idempotency
            if event_id in self._seen:
                return
            if len(self._seen) >= self._max_seen:
                self._seen.clear()
            self._seen.add(event_id)

        event = body.get("event") or {}
        thread_ts = event.get("thread_ts") or event.get("ts")
        task = _MENTION_RE.sub("", event.get("text") or "").strip()
        if not task:
            await say(text="⚠️ 작업 내용이 비어 있습니다. `@bot <작업 설명>` 형식으로 요청하세요.",
                      thread_ts=thread_ts)
            return

        note = " (앞선 요청 완료 후 순차 실행됩니다)" if self.runner.busy else ""
        await say(text=f"⏳ 접수: {task}{note}", thread_ts=thread_ts)
        try:
            execution_id, result, repo = await self.runner.run(task)
            await say(text=format_result(execution_id, result, repo), thread_ts=thread_ts)
        except RepoRegistryError as e:
            await say(text=f"⚠️ {e}", thread_ts=thread_ts)
        except asyncio.TimeoutError:
            await say(text=f"⏰ 타임아웃({self.runner.timeout:.0f}s) — 실행을 중단했습니다.",
                      thread_ts=thread_ts)
        except Exception as e:                          # 엔진 예외는 스레드로 회신
            await say(text=f"💥 실행 실패: {type(e).__name__}: {e}", thread_ts=thread_ts)


async def _amain() -> None:
    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
    from slack_bolt.async_app import AsyncApp

    bot_token = os.environ.get("SLACK_BOT_TOKEN")
    app_token = os.environ.get("SLACK_APP_TOKEN")
    if not bot_token or not app_token:                 # fail-fast, 조용한 기본값 금지
        raise SystemExit("SLACK_BOT_TOKEN / SLACK_APP_TOKEN 환경변수가 필요합니다")

    app = AsyncApp(token=bot_token)
    runner = EngineRunner()
    handler = MentionHandler(runner)

    @app.event("app_mention")
    async def on_mention(body, say):
        await handler(body, say)

    @app.event("message")
    async def on_message(body, logger):                # crew는 mention만 받는다
        pass

    tasks = [AsyncSocketModeHandler(app, app_token).start_async()]

    # --- @brain (선택) — 토큰이 있으면 인터뷰 앱을 같은 프로세스에 함께 띄운다 ---
    brain_bot = os.environ.get("BRAIN_BOT_TOKEN")
    brain_app_token = os.environ.get("BRAIN_APP_TOKEN")
    if brain_bot and brain_app_token:
        from .slack_brain import BrainHandler

        brain_app = AsyncApp(token=brain_bot)

        async def crew_dispatch(task: str, channel: str, interview_ts: str,
                                brief_text: str) -> None:
            """핸드오프: 채널(스레드 밖)에 🧠→🛠 게시 후 crew를 in-process로 실행.

            실제 <@crew> 멘션 이벤트에 의존하지 않는다 — 봇 메시지의 이벤트 전달은
            보장이 없고, 전달되면 이중 실행이 된다. 게시는 기록용, 실행은 직접 호출."""
            resp = await brain_app.client.chat_postMessage(
                channel=channel,
                text=f"🧠→🛠 *brain → crew 작업 인계*\n{brief_text}\n"
                     f"_(인터뷰 스레드: {interview_ts})_")
            handoff_ts = resp["ts"]

            async def crew_say(*, text: str, thread_ts=None):
                await app.client.chat_postMessage(channel=channel, text=text,
                                                  thread_ts=thread_ts or handoff_ts)

            await handler({"event": {"text": task, "ts": handoff_ts}}, crew_say)

        brain = BrainHandler(runner.orch, runner.cfg, runner.repos, crew_dispatch)

        @brain_app.event("app_mention")
        async def on_brain_mention(body, say):
            await brain.on_mention(body, say)

        @brain_app.event("message")
        async def on_brain_message(body, say):         # 진행 중 인터뷰 스레드 답글만 반응
            await brain.on_thread_message(body, say)

        tasks.append(AsyncSocketModeHandler(brain_app, brain_app_token).start_async())
        print("devcrew: @brain 인터뷰 앱 활성화")

    print("devcrew slack engine: Socket Mode 연결 중… (@mention으로 작업을 요청하세요)")
    await asyncio.gather(*tasks)


def main() -> None:
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
