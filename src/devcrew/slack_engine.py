"""Slack → WorkflowEngine 브리지 (§12.1 축약) — Socket Mode로 mention을 받아
DEFAULT_TEMPLATE 워크플로를 실행하고 스레드로 결과를 회신한다.

실행: `uv run python -m devcrew.slack_engine`
필수 env: SLACK_BOT_TOKEN(xoxb), SLACK_APP_TOKEN(xapp, connections:write)
선택(브레인): BRAIN_BOT_TOKEN, BRAIN_APP_TOKEN — 있으면 @brain 인터뷰 앱도 함께 기동
선택 env:
  DEVCREW_TARGET_REPO   실행 대상 git repo 경로 (registry 접두 없을 때의 기본값)
  DEVCREW_RUNTIME_DIR   trace.db/harness.db 위치 (기본 .devcrew-runtime/)
  DEVCREW_ORCH_TIER     ORCHESTRATOR 결정 세션 tier override (기본: config = opus HIGH)
  DEVCREW_TASK_TIMEOUT  요청당 타임아웃 초 (기본 600)

정책: 같은 Slack 스레드의 후속 요청은 작업 공간과 워커/leader 세션을 이어받는다
(in-process) — "이어서 고쳐줘"에 코드베이스를 다시 탐색하지 않는다. 스레드에서
repo를 바꾸면 이월을 끊고 새로 시작한다. `repo명: 작업` 접두로 config/repos.yaml registry의 repo를 지정한다. 등록
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

from .repos import RepoRegistryError, load_repo_bases, load_repos, split_repo_target
from .usage import context_badge, measure
from .worktree import WorktreeManager

_MENTION_RE = re.compile(r"<@[A-Z0-9]+>")

# Slack이 `text`에서 확장하는 브로드캐스트 토큰. leader 보고문은 결국 워커의
# (신뢰 불가) 보고를 재료로 쓰므로, 채널 전체 알림을 유발하는 토큰은 무력화한다.
_BROADCAST_RE = re.compile(r"<!(channel|here|everyone)(\|[^>]*)?>", re.I)


# 저장소 루트 — `src/devcrew/slack_engine.py` 기준 두 단계 위
REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = REPO_ROOT / ".env"
LOADED_ENV_FILE: Path | None = None   # 실제로 읽은 파일 (기동 로그용)


def env_candidates(root: Path | str | None = None) -> list[Path]:
    """`.env`를 찾을 자리 — 워크트리 우선, 그다음 **본체 저장소**.

    워크트리는 설계상 버려지는 공간이다. 거기에만 비밀을 두면 정리 도구가 한 번 돌 때
    함께 사라진다 (2026-08-22에 실제로 그렇게 잃었다). 본체 저장소의 `.env`를 읽게 해
    워크트리를 몇 개를 만들고 지우든 토큰은 한 자리에 남게 한다.

    워크트리에 따로 두면 그쪽이 이긴다 — 실험용 토큰을 격리할 수 있어야 한다."""
    base = Path(root) if root else REPO_ROOT
    out = [base / ".env"]
    git = base / ".git"
    # 워크트리의 `.git`은 디렉토리가 아니라 `gitdir: <본체>/.git/worktrees/<이름>` 파일이다
    if git.is_file():
        try:
            line = git.read_text().strip()
        except OSError:
            return out
        marker = "/.git/worktrees/"
        if line.startswith("gitdir:") and marker in line:
            main_root = Path(line.split(":", 1)[1].strip().split(marker)[0])
            if main_root != base:
                out.append(main_root / ".env")
    return out


def load_env(path: Path | str | None = None) -> bool:
    """`.env`의 값으로 **비어 있는** 환경변수만 채운다. 반환: 파일을 읽었는가.

    셸에서 이미 export한 값은 덮지 않는다 — 임시로 토큰을 바꿔 띄우는 흐름을
    파일이 조용히 되돌리면 디버깅이 불가능해진다. 파일이 없어도 오류가 아니다
    (export만 쓰던 기존 방식이 그대로 돈다)."""
    paths = [Path(path)] if path else env_candidates()
    p = next((c for c in paths if c.is_file()), None)
    if p is None:
        return False
    globals()["LOADED_ENV_FILE"] = p
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value
    return True


def sanitize_slack(text: str) -> str:
    return _BROADCAST_RE.sub(lambda m: f"@{m.group(1).lower()}", text or "")


# 예산 경보 메시지에 붙는 중지 버튼의 action_id (bolt 핸들러가 이 이름으로 받는다)
STOP_ACTION = "engine_stop"


def stop_blocks(execution_id: str, warning: str) -> list[dict]:
    """예산 경보 + 중지 버튼 Block Kit. 경보는 종료 사유가 아니므로 실행은 계속되고,
    멈출지 말지는 버튼으로 사람이 정한다."""
    return [
        {"type": "section", "text": {"type": "mrkdwn",
         "text": f"⚠️ *{execution_id} 예산 경보*\n{warning}"}},
        {"type": "context", "elements": [{"type": "mrkdwn",
         "text": "실행은 계속됩니다. 중지하면 진행 중인 단계가 끝난 뒤 멈춥니다."}]},
        {"type": "actions", "elements": [{
            "type": "button", "style": "danger",
            "text": {"type": "plain_text", "text": "🛑 실행 중지", "emoji": True},
            "action_id": STOP_ACTION, "value": execution_id}]},
    ]


def stopped_blocks(execution_id: str) -> list[dict]:
    """중지 요청 후 버튼을 걷어낸 형태 (중복 클릭 방지)."""
    return [{"type": "section", "text": {"type": "mrkdwn",
             "text": f"🛑 *{execution_id} 중지 요청됨* — 진행 중인 단계가 끝나면 멈춥니다."}}]


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


def progress_text(events: list, elapsed: float) -> str:
    """trace 이벤트 → 진행 상태 한 줄 (assistant status용, 순수 함수)."""
    spawns = [e for e in events if e["event_type"] == "ModelRoutingEvent"]
    trans = [e for e in events if e["event_type"] == "NodeTransitionEvent"]
    decisions = [e for e in events if e["event_type"] == "DecisionEvent"]
    current = spawns[-1]["payload"]["role"] if spawns else "준비"
    parts = [f"{current} 실행 중 ({int(elapsed)}s)"]
    if trans:
        done = " → ".join(f"{e['payload']['node_id']}:{e['payload']['transition']}"
                          for e in trans[-3:])
        parts.append(done)
    if decisions:
        parts.append(f"결정 {len(decisions)}회")
    return " · ".join(parts)


def format_result(execution_id: str, result, repo: str) -> str:
    """ExecutionResult → Slack 회신 텍스트.

    본문은 crew leader가 쓴 보고문(`result.report`)이다 — 실행 전체를 본 관점에서
    사용자 언어로 쓴 글이 기계적 상태 덤프보다 읽힌다. 경로·사유·토큰 같은 하네스
    수치는 각주로 내려 붙인다. 보고문이 없으면(leader 세션 없음·생성 실패) 종전의
    기계 요약만 나간다 — 보고 실패가 결과 전달을 막지 않는다.
    """
    icon = {"COMPLETED": "✅", "NEEDS_HUMAN": "🙋", "ABORTED": "❌",
            "STOPPED": "🛑"}.get(result.status, "❓")
    report = sanitize_slack(str(getattr(result, "report", "") or "").strip())
    badge = context_badge(getattr(result, "context_used", 0),
                          getattr(result, "context_window", 0),
                          pct=(getattr(result, "context_pct", 0.0) or None))
    blocks = [badge] if badge else []
    blocks.append(f"{icon} *{execution_id}*" if report
                  else f"{icon} *{execution_id} {result.status}*")
    if report:
        blocks.append(report)
    for w in getattr(result, "warnings", None) or []:
        blocks.append(f"⚠️ {w}")

    hops = list(getattr(result, "path", None) or
                [f"{h['node_id']}:{h['transition']}" for h in result.node_history])
    foot = [f"*경로* `{' → '.join(hops) or '(없음)'}`"]
    reason = str(getattr(result, "reason", "") or "").strip()
    if reason:
        foot.append(f"*사유* {reason}")
    tail = f"결정 {result.decisions}회 · 토큰 {result.total_tokens:,}"
    by_role = getattr(result, "role_tokens", None) or {}
    if by_role:
        tail += " — " + " · ".join(
            f"{r} {t:,}" for r, t in sorted(by_role.items(), key=lambda kv: -kv[1]))
    foot.append(f"_{tail}_")
    foot.append(f"*작업 공간* {repo}")
    blocks.append("\n".join(foot))
    return "\n\n".join(blocks)


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
        # 사용자 결정(2026-08-19): 결정 세션도 config 기본(opus HIGH)을 따른다.
        # DEVCREW_ORCH_TIER를 명시한 경우에만 override.
        orch_tier = os.environ.get("DEVCREW_ORCH_TIER")
        if orch_tier:
            role_defaults = dict(cfg.role_defaults)
            role_defaults[Role.ORCHESTRATOR] = dataclasses.replace(
                role_defaults[Role.ORCHESTRATOR], tier=orch_tier)
            cfg = dataclasses.replace(cfg, role_defaults=role_defaults)
        self.cfg = cfg
        self.mcp = {"harness": build_harness_mcp(self.trace)}
        self.timeout = float(os.environ.get("DEVCREW_TASK_TIMEOUT", "600"))
        self.repos = load_repos()
        self.repo_bases = load_repo_bases()
        self._locks: dict[str, asyncio.Lock] = {}
        # execution_id는 프로세스 재시작 때 1부터 리셋되면 trace에서 과거 실행과
        # 충돌한다 — 기존 trace의 최대 번호 다음부터 이어 붙인다.
        self._seq = itertools.count(self._next_seq_start())
        # Slack 스레드 단위 상태: 작업 공간 + 노드 세션 이월(carry) + leader 세션
        self._threads: dict[str, dict] = {}
        # 사용자가 중지를 요청한 execution_id — 엔진이 노드 경계에서 확인한다
        self._stop_requests: set[str] = set()

    def _next_seq_start(self) -> int:
        top = 0
        for e in self.trace.events(event_type="ModelRoutingEvent"):
            ex = e.get("execution_id") or ""
            if ex.startswith("SLACK-") and ex[6:].isdigit():
                top = max(top, int(ex[6:]))
        return top + 1

    def request_stop(self, execution_id: str) -> None:
        self._stop_requests.add(execution_id)

    def busy_for(self, thread_key: str) -> bool:
        """이 스레드가 쓰는 repo에서 실행이 도는 중인가 (초기화 거부 판단용)."""
        st = self._threads.get(thread_key)
        if st is None:
            return False
        lock = self._locks.get(st.get("repo_name") or "_toy")
        return bool(lock and lock.locked())

    async def clear_thread(self, thread_key: str) -> dict | None:
        """스레드의 이월 컨텍스트를 버린다 — 노드 세션·leader 세션을 archive하고
        스레드 상태를 지운다. 다음 요청은 새 작업 공간과 새 세션에서 시작한다.

        worktree와 그 안의 커밋은 지우지 않는다 — 이미 만든 산출물은 사용자 자산이다.
        비운 적이 있다는 사실은 trace에 남긴다 (토큰 급감의 원인 추적용)."""
        st = self._threads.pop(thread_key, None)
        if st is None:
            return None
        carried = list((st.get("nodes") or {}).values())
        leader = st.get("leader") or {}
        if leader.get("sid"):
            carried.append({"inst": leader.get("inst"), "session_id": leader["sid"]})
        for entry in carried:
            inst, sid = entry.get("inst"), entry.get("session_id")
            if not (inst and sid):
                continue
            try:
                await self.orch.adapters[inst.provider].archive(sid)
            except Exception:
                pass                      # 이미 죽은 세션 — 정리는 best-effort
            try:
                self.orch.registry.finish(inst.instance_id)
            except Exception:
                pass
        try:
            self.trace.append("ThreadContextClearedEvent", task_id=thread_key,
                              payload={"repo": st.get("repo_name"),
                                       "sessions": len(carried),
                                       "where": st.get("where")})
        except Exception:
            pass
        return st

    def _lock_for(self, key: str) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    @property
    def busy(self) -> bool:
        return any(l.locked() for l in self._locks.values())

    async def run(self, task: str, *, on_progress=None, on_warning=None,
                  thread_key: str | None = None) -> tuple[str, object, str]:
        """task 1건 실행 → (execution_id, ExecutionResult, repo 경로).

        on_progress: async (text) — 지정 시 4초 간격으로 진행 상태 텍스트를 보낸다
        (trace 이벤트 폴링 기반 — 엔진 코어 변경 없음)."""
        from .decision import make_final_report, make_llm_decide
        from .engine import WorkflowEngine
        from .workflow import DEFAULT_TEMPLATE

        # 오타·잘못된 브랜치는 여기서 fail-fast. `repo@브랜치:`의 브랜치는 이번
        # 요청에 한해 repos.yaml의 고정 base를 덮어쓴다.
        repo_name, req_branch, task = split_repo_target(task, self.repos)
        # 같은 repo는 직렬(worktree 브랜치 경합·리뷰 혼선 방지), 다른 repo·toy는 병렬
        async with self._lock_for(repo_name or "_toy"):
            n = next(self._seq)
            execution_id = f"SLACK-{n}"
            st = self._threads.get(thread_key) if thread_key else None
            if st is not None and (st["repo_name"] != repo_name
                                   or (req_branch and st.get("base") != req_branch)):
                # 스레드 중간에 대상 repo나 base 브랜치가 바뀌었다 — 세션/작업 공간을
                # 이어받으면 다른 커밋의 worktree를 가리키는 세션이 되므로 끊는다.
                st = None
            if st is None:
                if repo_name:
                    base = req_branch or self.repo_bases.get(repo_name, "HEAD")
                    wt = WorktreeManager(self.repos[repo_name]).create(f"slack-{n}", base)
                    workspace = str(wt)
                    where = f"{repo_name} · wt/slack-{n} ({base} 기준)\n{wt}"
                else:
                    workspace = os.environ.get("DEVCREW_TARGET_REPO") or str(make_toy_repo())
                    where = workspace
                st = {"repo_name": repo_name, "workspace": workspace, "where": where,
                      "base": base if repo_name else None, "nodes": {}, "leader": {}}
                if thread_key:
                    self._threads[thread_key] = st
            workspace, where = st["workspace"], st["where"]
            lc = self.cfg.leader_context
            decide = make_llm_decide(
                self.orch, self.cfg, mcp_servers=self.mcp,
                leader_state=st["leader"] if lc.persistent else None,
                compact_at=lc.compact_at if lc.persistent else None,
                compact_ratio=lc.compact_at_ratio if lc.persistent else None)

            async def _warn(warning: str) -> None:
                if on_warning is not None:
                    await on_warning(execution_id, warning)

            engine = WorkflowEngine(
                self.orch, self.cfg, decide_fn=decide, on_warning=_warn,
                stop_check=lambda: execution_id in self._stop_requests)
            poller = None
            if on_progress is not None:
                loop = asyncio.get_running_loop()
                started = loop.time()

                async def _poll():
                    while True:
                        await asyncio.sleep(4)
                        try:
                            evs = self.trace.events(execution_id=execution_id)
                            await on_progress(progress_text(evs, loop.time() - started))
                        except Exception:
                            pass                     # 상태 갱신은 best-effort

                poller = asyncio.create_task(_poll())
            try:
                result = await asyncio.wait_for(
                    engine.run(execution_id=execution_id, task=task, worktree=workspace,
                               carry=st["nodes"]),
                    timeout=self.timeout)
            finally:
                if poller is not None:
                    poller.cancel()
                self._stop_requests.discard(execution_id)
            # 사용자용 보고문은 실행 전체를 본 leader가 쓴다 (기계 덤프 대체)
            if on_progress is not None:
                await on_progress("결과 정리 중…")
            text, spent = await make_final_report(
                self.orch, st["leader"] if lc.persistent else None)({
                    "task": task, "status": result.status, "reason": result.reason,
                    "path": result.path, "warnings": result.warnings,
                    "workspace": where, "outcomes": result.outcomes})
            if text or spent:
                result = dataclasses.replace(
                    result, report=text or "", total_tokens=result.total_tokens + spent,
                    role_tokens={**result.role_tokens,
                                 "ORCHESTRATOR": result.role_tokens.get("ORCHESTRATOR", 0) + spent})
            # leader 창 점유는 사용자가 /clear 시점을 판단하는 근거다 — 회신에 싣는다.
            # 어댑터가 실제 값(`/context`)을 알면 그걸 쓰고, 없을 때만 추정으로 폴백한다.
            used, window, pct = int(st["leader"].get("context_used", 0)), 0, None
            if lc.persistent:
                window = lc.window_tokens
                leader_inst, leader_sid = st["leader"].get("inst"), st["leader"].get("sid")
                real = (await measure(self.orch.adapters[leader_inst.provider], leader_sid)
                        if leader_inst and leader_sid else None)
                if real:
                    used, window, pct = real["used"], real["window"], real.get("pct")
            result = dataclasses.replace(
                result, context_used=used, context_window=window,
                context_pct=pct if pct is not None else 0.0)
            return execution_id, result, where


def slack_posters(brain_client, crew_client):
    """핸드오프 게시(brain 봇) / crew 회신 게시(crew 봇) 어댑터.

    _amain의 bolt 클로저 안에 있으면 테스트가 닿지 못한다 — 이 저장소는 그런 배선을
    두 번 조용히 잃었다(2026-08-20). crew 회신은 **항상 고정된 root 스레드**로 간다:
    호출자가 넘긴 thread_ts를 따르면 재인계 때 스레드가 갈라진다."""
    async def post_handoff(channel: str, text: str, thread_ts) -> str:
        resp = await brain_client.chat_postMessage(channel=channel, text=text,
                                                   thread_ts=thread_ts)
        return resp["ts"]

    async def post_crew(channel: str, text: str, thread_ts: str, **kw):
        return await crew_client.chat_postMessage(channel=channel, text=text,
                                                  thread_ts=thread_ts, **kw)

    return post_handoff, post_crew


def make_tutor_say(client, channel: str, thread):
    """@tutor 응답 poster — 스레드에 달되 **채널에도 함께 게시한다**.

    스레드 답글은 어느 클라이언트에서도 채널 피드에 뜨지 않는다. 데스크톱은 멘션 직후
    스레드 패널이 열린 채라 보였을 뿐이고, 모바일에는 그 패널이 없어 회차 전체가 통째로
    보이지 않았다 (2026-08-24). 회차 상태의 키는 여전히 `thread_ts`이므로 이어 풀기·재개는
    그대로다 — 게시 위치만 넓힌다.

    `reply_broadcast`는 스레드 답글에만 유효하다. 스레드가 없으면 켜지 않는다.
    """
    async def say(*, text, thread_ts=None, blocks=None):
        ts = thread_ts or thread
        return await client.chat_postMessage(
            channel=channel, text=text, thread_ts=ts, blocks=blocks,
            reply_broadcast=bool(ts))
    return say


def make_tutor_action(tutor, client):
    """@tutor 버튼 클릭 핸들러. **클로저 밖**에 둔다 — `_amain` 안에 두면 배선이
    통째로 사라져도 테스트가 전부 초록이다 (lessons.md C1).

    고른 보기를 원 메시지에 남기고 버튼은 걷는다. 정오는 표시하지 않는다 —
    채점은 회차 끝에 한 번에 한다."""
    async def on_action(body: dict) -> None:
        ch = body["channel"]["id"]
        msg = body.get("message") or {}
        thread = msg.get("thread_ts") or msg.get("ts")
        value = body["actions"][0]["value"]

        say = make_tutor_say(client, ch, thread)

        async def strip():
            if ":" in value:
                mark = f"✅ 선택: {chr(65 + int(value.split(':')[1]))}"
            else:
                mark = "🔁 다시 채점 중"        # 재채점은 보기 선택이 아니다
            await client.chat_update(
                channel=ch, ts=msg["ts"], blocks=[],
                text=(msg.get("text") or "문항")[:2800] + f"\n\n{mark}")

        await tutor.on_answer(thread_ts=thread, value=value, say=say, strip=strip,
                              channel=ch, user=(body.get("user") or {}).get("id", ""))
    return on_action


# 문두의 "@tutor" 주소 지정 토큰 딱 하나만 지운다. `+`로 "한 개 이상"을 허용하면
# "<@U0BRV19AMLL> <@U999>가 왜 여기 나와?"처럼 질문 본문이 멘션으로 시작할 때 그
# 본문 멘션까지 선두로 취급해 같이 삼켜버린다 — `^`로 문자열 시작에 고정하되 토큰은
# 정확히 하나만 매치해서 본문 중간(혹은 바로 뒤)의 다른 사람 멘션은 건드리지 않는다.
_TUTOR_MENTION_PREFIX_RE = re.compile(r"^\s*<@[A-Z0-9]+>\s*")


def make_tutor_question(tutor, client):
    """스레드 후속 질문 라우터 (#19). `message`와 `app_mention` 둘 다 여기로 모은다.

    **봇 메시지와 subtype 붙은 메시지는 진입 전에 버린다.** tutor 응답은 채널에
    `reply_broadcast`로 게시되고 그건 다시 `message` 이벤트로 돌아온다 — 거르지 않으면
    봇이 자기 답변에 답하는 무한 루프가 된다.

    스레드 밖(최상위) 메시지도 버린다. 회차는 스레드 단위이므로 스레드가 없으면
    후속 질문일 수 없다.

    **스레드 안에서 `@tutor 질문`을 하면 Slack이 `app_mention`과 `message` 두 이벤트를
    서로 다른 `event_id`로 각각 보낸다.** `TutorHandler._dedupe`는 `event_id`로 걸러
    이 경우를 못 잡는다(애초에 이 라우터는 그걸 호출하지도 않는다) — 그래서 메시지
    자신의 정체성인 `(channel, ts)`로 직접 막는다. `app_mention` 분기를 없애서 풀 수도
    있어 보이지만, `docs/tutor-setup.md`가 현재 `app_mention` 구독만 안내하고 있어서
    그 설정의 워크스페이스에는 그게 유일한 경로다 — 지우면 기능이 통째로 사라진다.
    """
    seen: set[tuple[str, str]] = set()
    max_seen = 1000

    async def route(body: dict) -> None:
        ev = body.get("event") or {}
        if ev.get("bot_id") or ev.get("subtype"):
            return
        thread = ev.get("thread_ts")
        if not thread or thread == ev.get("ts"):
            return
        key = (ev.get("channel", ""), ev.get("ts", ""))
        if key in seen:
            return
        if len(seen) >= max_seen:
            seen.clear()
        seen.add(key)
        text = _TUTOR_MENTION_PREFIX_RE.sub("", ev.get("text") or "").strip()
        if not text:
            return
        ch = ev.get("channel", "")
        say = make_tutor_say(client, ch, thread)
        await tutor.on_question(thread_ts=thread, text=text,
                                user=ev.get("user") or "", say=say, channel=ch)
    return route


def make_tutor_dispatch(tutor, client, question=None):
    """`app_mention` 라우팅 결정 — 스레드 안의 회차에 대한 멘션이면 질문, 아니면 회차 시작.

    **클로저 밖**에 둔다. 이 분기가 `_amain` 안에 있으면 세 줄을 통째로 지워도 테스트가
    전부 초록이다 — 이 저장소가 배선을 두 번 조용히 잃은 그 자리다 (lessons.md C1).

    회차 유무를 보는 이유: **모든** in-thread 멘션을 질문으로 보내면 스레드 안에서
    `@tutor myrepo:` 로 새 회차를 시작하는 길이 사라진다. `on_question`은 회차가 없는
    스레드에서 조용히 무시하므로(그게 옳다 — 봇과 무관한 대화에 끼어들지 않는다)
    사용자에게는 무반응으로만 보인다. repo 접두사 검증은 `on_mention`이 이미 하므로
    여기서 겹쳐 하지 않는다.

    `question`은 `message` 핸들러가 쓰는 라우터와 **같은 인스턴스여야 한다.** 중복
    차단 상태(`(channel, ts)` set)가 그 클로저 안에 있어서, 따로 만들면 스레드 안의
    `@tutor 질문`이 app_mention과 message 두 이벤트로 각각 통과해 같은 질문에 답이
    두 번 나간다. 넘기지 않으면 여기서 하나 만든다(단독 사용 편의).
    """
    question = question or make_tutor_question(tutor, client)

    async def dispatch(body: dict) -> None:
        ev = body.get("event") or {}
        thread = ev.get("thread_ts")
        if thread and thread != ev.get("ts") and tutor.has_round(thread):
            await question(body)
            return
        say = make_tutor_say(client, ev.get("channel", ""), thread or ev.get("ts"))
        await tutor.on_mention(body, say)

    return dispatch


def make_crew_dispatch(post_handoff, post_crew, handler, roots: dict | None = None,
                       trace=None):
    """brain → crew 핸드오프 디스패처 (bolt 클라이언트와 분리된 순수 로직).

    같은 인터뷰의 재인계는 **첫 핸드오프 스레드로 되돌린다**. crew의 leader 세션 이월은
    thread_ts를 키로 하므로 재인계마다 새 채널 메시지를 만들면 컨텍스트가 매번 끊긴다 —
    스레드를 고정하면 재인계도, 그 스레드에 사용자가 직접 다는 후속 멘션도 같은 키가 된다.

    `trace`를 주면 그 고정을 append-only trace에도 남긴다 — 프로세스가 재시작돼도
    같은 인터뷰의 재인계가 새 채널 메시지로 갈라지지 않는다.

    post_handoff: async (channel, text, thread_ts|None) -> ts   (brain 봇이 게시)
    post_crew:    async (channel, text, thread_ts, **kw) -> any (crew 봇이 게시)
    """
    roots = {} if roots is None else roots

    def _remembered(interview_ts: str) -> str | None:
        root = roots.get(interview_ts)
        if trace is None:
            return root
        exec_id = f"BRAIN-{interview_ts}"
        try:
            evs = trace.events(event_type="CrewHandoffThreadEvent", execution_id=exec_id)
            closed = trace.events(event_type="BrainClosedEvent", execution_id=exec_id)
        except Exception:
            return root
        if not evs:
            return root
        if closed and closed[-1]["ts"] >= evs[-1]["ts"]:
            # 종료 후 같은 Slack 스레드에서 새 인터뷰가 시작될 수 있다 — 그 brief를
            # 이전 인터뷰의 crew 스레드에 붙이면 남의 작업 맥락을 물려받는다
            roots.pop(interview_ts, None)
            return None
        return root or evs[-1]["payload"].get("root")

    async def crew_dispatch(task: str, channel: str, interview_ts: str,
                            brief_text: str) -> None:
        root = _remembered(interview_ts)
        head = "🧠→🛠 *brain → crew 추가 인계*" if root else "🧠→🛠 *brain → crew 작업 인계*"
        ts = await post_handoff(
            channel, f"{head}\n{brief_text}\n_(인터뷰 스레드: {interview_ts})_", root)
        if root is None:
            root = ts
            if trace is not None:
                try:
                    trace.append("CrewHandoffThreadEvent", task_id=f"BRAIN-{interview_ts}",
                                 execution_id=f"BRAIN-{interview_ts}",
                                 payload={"root": root, "channel": channel})
                except Exception:
                    pass                   # 기록 실패는 이번 인계를 막지 않는다
        if len(roots) > 500:               # 프로세스 캐시일 뿐 — 진실은 trace에 있다
            roots.clear()
        roots[interview_ts] = root

        async def crew_say(*, text: str, thread_ts=None, **kw):
            return await post_crew(channel, text, root, **kw)

        await handler({"event": {"text": task, "ts": root, "thread_ts": root,
                                 "channel": channel}}, crew_say)

    return crew_dispatch


# `/clear`는 진짜 Slack 슬래시 커맨드가 아니라 멘션 뒤에 붙이는 토큰이다 — 슬래시
# 커맨드 페이로드에는 thread_ts가 없어 어느 스레드를 비울지 알 수 없다.
CLEAR_RE = re.compile(r"^/clear$", re.I)


class MentionHandler:
    """app_mention 이벤트 처리 — bolt와 분리된 순수 로직 (테스트 대상)."""

    def __init__(self, runner: EngineRunner, *, max_seen: int = 1000, react=None,
                 status=None):
        self.runner = runner
        self.react = react            # async (channel, ts) — 수신 확인 리액션 (선택)
        self.status = status          # async (channel, thread_ts, text) — AI 앱 상태 (선택)
        self._seen: set[str] = set()
        self._max_seen = max_seen

    async def _clear(self, thread_ts: str, say) -> None:
        if self.runner.busy_for(thread_ts):
            await say(text="⏳ 이 스레드에서 실행이 진행 중입니다. `🛑 실행 중지`로 먼저 "
                           "멈춘 뒤 다시 `/clear` 해주세요.", thread_ts=thread_ts)
            return
        st = await self.runner.clear_thread(thread_ts)
        if st is None:
            await say(text="ℹ️ 이 스레드에는 비울 crew 컨텍스트가 없습니다.",
                      thread_ts=thread_ts)
            return
        await say(text="🧹 이 스레드의 crew 컨텍스트를 비웠습니다 — leader와 각 단계 세션을 "
                       "모두 닫았습니다.\n다음 요청은 새 작업 공간에서 처음부터 시작합니다 "
                       "_(기존 worktree와 커밋은 그대로 둡니다)_.", thread_ts=thread_ts)

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
        if CLEAR_RE.match(task):
            await self._clear(thread_ts, say)
            return
        if not task:
            await say(text="⚠️ 작업 내용이 비어 있습니다. `@bot <작업 설명>` 형식으로 요청하세요.",
                      thread_ts=thread_ts)
            return

        channel = event.get("channel")
        if self.react and channel and event.get("ts"):
            try:
                await self.react(channel, event["ts"])
            except Exception:
                pass                   # reactions:write scope 없음 등 — 리액션은 best-effort

        # 상태는 메시지가 아니라 AI 앱 상태 인디케이터로 — 댓글은 최종 답변만 (사용자 UX 결정)
        async def _status(text):
            if self.status and channel and thread_ts:
                try:
                    await self.status(channel, thread_ts, text)
                except Exception:
                    pass               # Agents & AI Apps 미설정 등 — 상태는 best-effort

        async def _warn(execution_id: str, warning: str) -> None:
            # 경보는 종료 사유가 아니다 — 사람이 멈출 수 있도록 버튼만 함께 준다
            try:
                await say(text=f"⚠️ {execution_id} 예산 경보: {warning}",
                          blocks=stop_blocks(execution_id, warning), thread_ts=thread_ts)
            except Exception:
                pass                   # 알림 실패가 실행을 막지 않는다

        await _status("대기 중 (앞선 요청 처리 후 실행)" if self.runner.busy else "접수 — 실행 준비 중")
        try:
            execution_id, result, repo = await self.runner.run(
                task, on_progress=_status, on_warning=_warn, thread_key=thread_ts)
            await say(text=format_result(execution_id, result, repo), thread_ts=thread_ts)
        except RepoRegistryError as e:
            await say(text=f"⚠️ {e}", thread_ts=thread_ts)
        except asyncio.TimeoutError:
            await say(text=f"⏰ 타임아웃({self.runner.timeout:.0f}s) — 실행을 중단했습니다.",
                      thread_ts=thread_ts)
        except Exception as e:                          # 엔진 예외는 스레드로 회신
            await say(text=f"💥 실행 실패: {type(e).__name__}: {e}", thread_ts=thread_ts)


async def _amain() -> None:
    if load_env():
        print(f"devcrew: {LOADED_ENV_FILE} 로드 (셸 export 우선)")
    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
    from slack_bolt.async_app import AsyncApp

    bot_token = os.environ.get("SLACK_BOT_TOKEN")
    app_token = os.environ.get("SLACK_APP_TOKEN")
    if not bot_token or not app_token:                 # fail-fast, 조용한 기본값 금지
        raise SystemExit("SLACK_BOT_TOKEN / SLACK_APP_TOKEN 환경변수가 필요합니다")

    app = AsyncApp(token=bot_token)
    runner = EngineRunner()

    async def crew_react(channel: str, ts: str) -> None:
        await app.client.reactions_add(channel=channel, timestamp=ts, name="eyes")

    async def crew_status(channel: str, thread_ts: str, text: str) -> None:
        await app.client.assistant_threads_setStatus(
            channel_id=channel, thread_ts=thread_ts, status=text)

    handler = MentionHandler(runner, react=crew_react, status=crew_status)

    @app.event("app_mention")
    async def on_mention(body, say):
        await handler(body, say)

    @app.action(STOP_ACTION)
    async def on_stop(ack, body, respond):
        await ack()
        execution_id = ((body.get("actions") or [{}])[0]).get("value") or ""
        runner.request_stop(execution_id)
        # 버튼을 걷어내 중복 클릭을 막고, 중지 요청 상태를 그 자리에 남긴다
        await respond(replace_original=True, text=f"🛑 {execution_id} 중지 요청됨",
                      blocks=stopped_blocks(execution_id))

    @app.event("message")
    async def on_message(body, logger):                # crew는 mention만 받는다
        pass

    tasks = [AsyncSocketModeHandler(app, app_token).start_async()]

    # --- @brain (선택) — 토큰이 있으면 인터뷰 앱을 같은 프로세스에 함께 띄운다 ---
    def _real_token(v: str | None, prefix: str) -> str | None:
        # .env 템플릿의 자리표시자("여기에-붙여넣기")는 미설정으로 취급한다
        if not v or not v.startswith(prefix) or "붙여넣기" in v:
            return None
        return v

    brain_bot = _real_token(os.environ.get("BRAIN_BOT_TOKEN"), "xoxb-")
    brain_app_token = _real_token(os.environ.get("BRAIN_APP_TOKEN"), "xapp-")
    if brain_bot and brain_app_token:
        from .slack_brain import BrainHandler

        brain_app = AsyncApp(token=brain_bot)

        # 핸드오프는 채널에 게시하되 crew 실행은 in-process 직접 호출이다 — 봇 메시지의
        # 멘션 이벤트 전달은 보장이 없고, 전달되면 이중 실행이 된다.
        crew_dispatch = make_crew_dispatch(
            *slack_posters(brain_app.client, app.client), handler,
            trace=runner.orch.trace)

        async def brain_react(channel: str, ts: str) -> None:
            await brain_app.client.reactions_add(channel=channel, timestamp=ts, name="eyes")

        async def brain_status(channel: str, thread_ts: str, text: str) -> None:
            await brain_app.client.assistant_threads_setStatus(
                channel_id=channel, thread_ts=thread_ts, status=text)

        brain = BrainHandler(runner.orch, runner.cfg, runner.repos, crew_dispatch,
                             react=brain_react, status=brain_status)

        @brain_app.event("app_mention")
        async def on_brain_mention(body, say):
            await brain.on_mention(body, say)

        @brain_app.event("message")
        async def on_brain_message(body, say):         # 진행 중 인터뷰 스레드 답글만 반응
            await brain.on_thread_message(body, say)

        @brain_app.action(re.compile("brain_answer_.*"))
        async def on_brain_answer(ack, body):          # 스레드 내 선택지 버튼 클릭
            await ack()
            ch = body["channel"]["id"]
            msg = body.get("message") or {}
            thread = msg.get("thread_ts") or msg.get("ts")
            value = body["actions"][0]["value"]

            async def bsay(*, text, thread_ts=None, blocks=None):
                return await brain_app.client.chat_postMessage(
                    channel=ch, text=text, thread_ts=thread_ts or thread, blocks=blocks)

            async def strip():
                await brain_app.client.chat_update(
                    channel=ch, ts=msg["ts"], blocks=[],
                    text=(msg.get("text") or "질문")[:2800] + f"\n\n✅ 선택: {value}")

            await brain.on_answer(thread_ts=thread, value=value, say=bsay, strip=strip,
                                  channel=ch, user=(body.get("user") or {}).get("id", ""))

        tasks.append(AsyncSocketModeHandler(brain_app, brain_app_token).start_async())
        print("devcrew: @brain 인터뷰 앱 활성화")

    tutor_bot = _real_token(os.environ.get("TUTOR_BOT_TOKEN"), "xoxb-")
    tutor_app_token = _real_token(os.environ.get("TUTOR_APP_TOKEN"), "xapp-")
    if tutor_bot and tutor_app_token:
        from .slack_tutor import TutorHandler

        tutor_app = AsyncApp(token=tutor_bot)

        async def tutor_react(channel: str, ts: str) -> None:
            await tutor_app.client.reactions_add(channel=channel, timestamp=ts,
                                                 name="mortar_board")

        async def tutor_status(channel: str, thread_ts: str, text: str) -> None:
            await tutor_app.client.assistant_threads_setStatus(
                channel_id=channel, thread_ts=thread_ts, status=text)

        tutor = TutorHandler(runner.orch, runner.cfg, runner.repos,
                             react=tutor_react, status=tutor_status)
        tutor_action = make_tutor_action(tutor, tutor_app.client)
        tutor_question = make_tutor_question(tutor, tutor_app.client)
        # 같은 라우터 인스턴스를 넘긴다 — 중복 차단 상태를 공유해야 app_mention과
        # message로 두 번 들어오는 같은 질문에 답이 두 번 나가지 않는다.
        tutor_dispatch = make_tutor_dispatch(tutor, tutor_app.client, tutor_question)

        @tutor_app.event("app_mention")
        async def on_tutor_mention(body, say):
            # 분기(질문이냐 새 회차냐)는 `make_tutor_dispatch`가 한다 — 클로저 안에
            # 두면 배선이 통째로 사라져도 테스트가 전부 초록이다 (lessons.md C1).
            await tutor_dispatch(body)

        @tutor_app.event("message")
        async def on_tutor_message(body, say):
            # 회차 진행(보기 선택)은 버튼으로만 받는다. 자유 답글은 채점이 끝난
            # 회차의 **후속 질문**으로만 취급한다 (#19).
            await tutor_question(body)

        @tutor_app.action(re.compile("tutor_answer_.*"))
        async def on_tutor_answer(ack, body):
            await ack()
            await tutor_action(body)

        @tutor_app.action("tutor_report")
        async def on_tutor_report(ack):
            # 리포트 버튼은 `url` 버튼이라 여는 일은 클라이언트가 한다. 그래도 Slack은
            # interaction을 보내고, 3초 안에 응답이 없으면 사용자에게 경고를 띄운다 —
            # 여기서 하는 일은 ack뿐이지만 없으면 성공 경로가 실패처럼 보인다.
            await ack()

        tasks.append(AsyncSocketModeHandler(tutor_app, tutor_app_token).start_async())
        print("devcrew: @tutor 학습 앱 활성화")

    print("devcrew slack engine: Socket Mode 연결 중… (@mention으로 작업을 요청하세요)")
    await asyncio.gather(*tasks)


def main() -> None:
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
