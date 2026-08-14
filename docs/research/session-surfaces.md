# Claude Code · Codex 세션을 Python에서 구동하는 표면 조사

> GitHub issue #2 대응 조사 문서. 모든 주장은 1차 출처(공식 문서 / 공식 저장소 / CLI 자체 `--help` 출력)로 근거를 표기한다.
> 조사 기준일: 2026-08-14. 검증에 사용한 로컬 바이너리: `claude` v2.1.232, `codex-cli` 0.145.0.

## 0. 요약

| 항목 | Claude Code | Codex |
| --- | --- | --- |
| 공식 Python SDK | **있음** — `claude-agent-sdk` (`claude_agent_sdk`) | **있음** — `openai-codex` (Python 3.10+, stable) |
| 1차 권장 표면 | Agent SDK for Python (`ClaudeSDKClient`) | Codex Python SDK (`Codex` / `AsyncCodex`) |
| 대체 표면 | headless CLI (`claude -p`) subprocess | `codex exec` CLI subprocess |
| 세션 단위 명칭 | session (`session_id`, UUID) | thread (`thread_id`) |
| `archive` 네이티브 지원 | ❌ (없음, 우회 필요) | ✅ `thread_archive` / `codex archive` |
| `cancel` 네이티브 지원 | ✅ streaming 모드 한정 (`interrupt()`) | ✅ `TurnHandle.interrupt()` |

**설계에 직결되는 핵심 결론 3가지**

1. Codex는 CLI subprocess가 유일한 표면이 아니다. `pip install openai-codex`로 설치되는 공식 Python SDK가 local Codex **app-server를 JSON-RPC로 제어**하며, DESIGN.md 6.2의 6개 어댑터 연산을 **전부** 1:1로 커버한다. Reviewer adapter는 subprocess glue 없이 구현 가능하다.
2. Claude Code 쪽은 `archive(sessionId)`에 대응하는 네이티브 API가 없다. `tag_session()` + transcript 파일 이동으로 우회해야 한다.
3. `effortLevel: LOW | MEDIUM | HIGH | MAX` 공통 표현은 두 provider에 **비대칭**으로 매핑된다. Codex에는 `max`가 없고 Claude Code에는 `minimal`이 없다. DESIGN.md 6.1이 요구하는 "지원하지 않는 조합은 routing validation error" 규칙이 실제로 필요한 지점이다.

---

## 1. Claude Code

### 1.1 표면 A — Claude Agent SDK for Python

설치는 `pip install claude-agent-sdk` (또는 `uv add claude-agent-sdk`), Python 3.10+. import 이름은 `claude_agent_sdk`. **SDK 패키지가 native Claude Code 바이너리를 번들**하므로 대부분의 환경에서 별도 Claude Code 설치가 불필요하다 — 단, pip이 platform wheel 대신 source distribution을 설치하는 경우(예: ARM64 Windows) 바이너리가 없으며, 이때는 Claude Code를 네이티브 설치하면 SDK가 `PATH`에서 찾는다. ([quickstart](https://code.claude.com/docs/en/agent-sdk/quickstart))

두 가지 진입점이 있다. ([python reference](https://code.claude.com/docs/en/agent-sdk/python))

- `query(*, prompt, options, transport) -> AsyncIterator[Message]` — 호출마다 새 세션을 만드는 one-shot 경로.
- `ClaudeSDKClient(options, transport)` — 여러 turn에 걸쳐 세션을 유지하는 클래스.

공식 비교표는 `query()`가 **Interrupts를 지원하지 않고**(❌) `ClaudeSDKClient`만 지원한다고(✅) 명시한다. ([python reference](https://code.claude.com/docs/en/agent-sdk/python)) 따라서 harness의 adapter는 `ClaudeSDKClient` 기반이어야 한다.

`ClaudeSDKClient`의 메서드 시그니처: ([python reference](https://code.claude.com/docs/en/agent-sdk/python))

```python
async def connect(self, prompt: str | AsyncIterable[dict] | None = None) -> None
async def query(self, prompt: str | AsyncIterable[dict], session_id: str = "default") -> None
async def receive_messages(self) -> AsyncIterator[Message]
async def receive_response(self) -> AsyncIterator[Message]   # ResultMessage까지 소비
async def interrupt(self) -> None                            # streaming 모드 전용
async def set_permission_mode(self, mode: str) -> None
async def set_model(self, model: str | None = None) -> None
async def stop_task(self, task_id: str) -> None
async def get_server_info(self) -> dict[str, Any] | None     # session ID + capabilities
async def disconnect(self) -> None
```

세션 관리용 모듈 레벨 함수도 별도로 제공된다: `list_sessions(directory, limit, offset, include_worktrees)`, `get_session_messages(session_id, directory, limit, offset)`, `get_session_info(session_id, directory)`, `rename_session(session_id, title, directory)`, `tag_session(session_id, tag, directory)`. ([python reference](https://code.claude.com/docs/en/agent-sdk/python), [sessions](https://code.claude.com/docs/en/agent-sdk/sessions))

#### 어댑터 연산 매핑

| 연산 | 지원 | 방법 |
| --- | --- | --- |
| `startSession` | ✅ | `ClaudeSDKClient(options).connect()`. `ClaudeAgentOptions.session_id`로 harness가 UUID를 미리 지정할 수 있고, 지정하지 않으면 `ResultMessage.session_id`에서 회수한다. |
| `send` | ✅ | 같은 client 인스턴스에 `await client.query(prompt)` 재호출. 두 번째 query가 첫 번째의 컨텍스트를 그대로 유지한다는 것이 공식 예제로 명시되어 있다. |
| `resume` | ✅ | `ClaudeAgentOptions(resume=<session_id>)`. 부가 옵션: `continue_conversation`(가장 최근 세션), `fork_session`(새 ID로 분기), `resume_session_at`(특정 message UUID까지만 로드), `resume_drops_turn`. |
| `cancel` | ✅ (조건부) | `await client.interrupt()`. **streaming 모드 전용**이며 `query()` 경로에서는 불가. 프로세스 레벨 취소는 SIGTERM(§1.2 참조). 백그라운드 task 단위 중단은 `stop_task(task_id)`. |
| `archive` | ❌ → 우회 | archive/unarchive API가 없다. 대안: `tag_session(session_id, tag)` + `rename_session()`으로 논리적 상태 표시, 물리적으로는 `~/.claude/projects/<encoded-cwd>/<session-id>.jsonl` 파일을 harness 소유 디렉터리로 이동. 단 transcript 엔트리 포맷은 "internal to Claude Code and changes between versions"로 명시되어 있어 **직접 파싱은 금지**, 이동/보관 용도로만 쓸 것. |
| `getUsage` | ✅ | `ResultMessage.usage`(`input_tokens`, `output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`), `ResultMessage.total_cost_usd`, `ResultMessage.model_usage`(모델별 분해), `ResultMessage.terminal_reason`, `ResultMessage.session_id`. |

출처: [python reference](https://code.claude.com/docs/en/agent-sdk/python), [Work with sessions](https://code.claude.com/docs/en/agent-sdk/sessions), [Manage sessions](https://code.claude.com/docs/en/sessions), [Track cost and usage](https://code.claude.com/docs/en/agent-sdk/cost-tracking)

#### model / effort 지정

`ClaudeAgentOptions`의 관련 필드: ([python reference](https://code.claude.com/docs/en/agent-sdk/python))

- `model: str | None` — alias(`sonnet`, `opus`, `haiku`, `fable`) 또는 full model name
- `fallback_model: str | None`
- `effort: EffortLevel | None` — `Literal["low", "medium", "high", "xhigh", "max"]`
- `thinking: ThinkingConfig | None` — `{"type": "adaptive"|"enabled"|"disabled", "budget_tokens": int, "display": "summarized"}` (TypedDict, 런타임에는 plain dict)
- `max_thinking_tokens` — **deprecated**, `thinking` 사용

세션 도중 변경은 `await client.set_model(model)`로 가능하다. subagent 단위로도 `AgentDefinition.model` / `AgentDefinition.effort`를 따로 줄 수 있다(단 `AgentDefinition`은 camelCase 필드를 쓴다).

#### 비용/사용량 데이터의 신뢰도 (중요)

공식 문서가 명시적으로 경고한다: `total_cost_usd`와 `costUSD`는 **client-side estimate**이며 빌드 시점에 번들된 가격표에서 로컬 계산된다. 가격 변경, SDK가 모르는 모델, 클라이언트가 모델링할 수 없는 청구 규칙에서 실제 청구와 어긋날 수 있다. "Do not bill end users or trigger financial decisions from these fields." ([cost-tracking](https://code.claude.com/docs/en/agent-sdk/cost-tracking))

또한 subagent를 쓰면 필드별로 집계 범위가 다르다: ([cost-tracking](https://code.claude.com/docs/en/agent-sdk/cost-tracking))

| 필드 | subagent 사용량 포함 여부 |
| --- | --- |
| `usage` | **제외** (top-level agent loop만) |
| `total_cost_usd` | 포함 |
| `model_usage` | 포함 (모델별 분해) |

dev-crew는 subagent를 적극적으로 쓰는 구조이므로 **`usage`가 아니라 `model_usage`를 whole-tree 토큰 회계의 기준으로 삼아야 한다.**

추가 주의: assistant message의 per-step `output_tokens`는 placeholder다(API가 `message_start` 시점에 보고한 값). 실제 output token은 result message의 `usage`에서 읽어야 한다. 그리고 세션 크래시 시 emit되는 `error_during_execution` result는 모든 cost 필드가 0으로 채워질 수 있어, 직전 result에서 누계를 복구해야 한다. ([cost-tracking](https://code.claude.com/docs/en/agent-sdk/cost-tracking))

---

### 1.2 표면 B — headless CLI (`claude -p`)

CLI를 subprocess로 구동하는 경로. Agent SDK Python 패키지도 내부적으로 이 CLI를 감싼다(`ClaudeAgentOptions.cli_path`로 바이너리 경로 지정 가능).

핵심 플래그 ([CLI reference](https://code.claude.com/docs/en/cli-reference), [Run Claude Code programmatically](https://code.claude.com/docs/en/headless), 로컬 `claude --help` v2.1.232):

| 플래그 | 의미 |
| --- | --- |
| `-p`, `--print` | 비대화형 실행 |
| `--output-format <text\|json\|stream-json>` | 출력 포맷 |
| `--input-format <text\|stream-json>` | 입력 포맷. `stream-json`이 realtime streaming input |
| `-r`, `--resume <id\|name>` | 세션 ID 또는 이름으로 재개 |
| `-c`, `--continue` | 현재 디렉터리의 최근 대화 이어받기 |
| `--fork-session` | resume/continue 시 새 session ID로 분기 |
| `--session-id <uuid>` | 세션 ID를 호출자가 지정 (유효한 UUID) |
| `--model <alias\|full>` | `sonnet` / `opus` / `haiku` / `fable` 또는 full name |
| `--effort <level>` | `low`, `medium`, `high`, `xhigh`, `max`, `ultracode` |
| `--max-turns <n>` | agentic turn 상한 (print 모드 전용, 초과 시 error exit) |
| `--max-budget-usd <amount>` | 지출 상한 (print 모드 전용) |
| `--permission-mode <mode>` | `default`, `acceptEdits`, `plan`, `auto`, `dontAsk`, `bypassPermissions`, `manual` |
| `--allowedTools "Bash,Read,Edit"` | 도구 자동 승인 (permission rule syntax) |
| `--json-schema <schema>` | 구조화 출력 스키마 → 결과는 `structured_output` 필드 |
| `--include-partial-messages` | 토큰 단위 stream event 포함 (`--print` + `stream-json` 필요) |
| `--replay-user-messages` | stdin의 user message를 stdout으로 되돌려 ack (양쪽 `stream-json` 필요) |
| `--forward-subagent-text` | subagent의 text/thinking 블록도 스트림에 emit |
| `--bare` | hooks/skills/plugins/MCP/CLAUDE.md 자동 탐색 생략, 시작 시간 단축 |
| `--no-session-persistence` | 이번 실행의 transcript 기록 억제 |
| `--agents <json>` | subagent를 JSON으로 동적 정의 |

#### 어댑터 연산 매핑

| 연산 | 지원 | 방법 |
| --- | --- | --- |
| `startSession` | ✅ | `claude -p "<prompt>" --output-format json --session-id <uuid>` 또는 ID 생략 후 응답의 `session_id`를 회수 |
| `send` | ✅ (두 방식) | (a) 프로세스 유지형: `--input-format stream-json --output-format stream-json`으로 stdin에 `{"type":"user","message":{"role":"user","content":"..."}}` JSONL을 계속 흘려보낸다. (b) 프로세스 단발형: `claude -p "<followup>" --resume "$session_id"` |
| `resume` | ✅ | `--resume <session-id>`. **프로세스 재시작 후에도 동작**. v2.1.223부터는 실행 디렉터리와 무관하게 머신 전체에서 ID를 찾는다(그 이전 버전은 현재 project 디렉터리와 git worktree로 한정). 단 session 파일이 **같은 머신에 존재해야 한다**. |
| `cancel` | ⚠️ 우회 | 전용 플래그 없음. SIGTERM(`kill`, process supervisor, SDK host의 세션 종료)을 보내면 진행 중 turn을 abort하고, 실행 중 Bash 명령의 process tree를 종료하고, `SessionEnd` hook을 실행한 뒤 **exit code 143**으로 종료한다. |
| `archive` | ❌ → 우회 | CLI에 archive 서브커맨드 없음. §1.1과 동일한 우회. |
| `getUsage` | ✅ | `--output-format json`의 응답 payload에 `result`, `session_id`, `total_cost_usd`, 모델별 cost breakdown이 포함된다. `stream-json`에서는 마지막 줄의 `result` 메시지가 최종 텍스트 + cost + session metadata를 담는다. |

출처: [CLI reference](https://code.claude.com/docs/en/cli-reference), [Run Claude Code programmatically](https://code.claude.com/docs/en/headless), [Streaming Input](https://code.claude.com/docs/en/agent-sdk/streaming-vs-single-mode), [Work with sessions](https://code.claude.com/docs/en/agent-sdk/sessions)

#### 스트림에서 얻을 수 있는 부가 신호

harness의 상태 머신에 유용한 이벤트가 `stream-json`에 이미 존재한다. ([headless](https://code.claude.com/docs/en/headless))

- `system/init` — 스트림 첫 이벤트. model, tools, MCP servers, loaded plugins, `plugins` / `plugin_errors`, `mcp_servers` / `mcp_server_errors`, 그리고 선택적 `capabilities` 문자열 배열(`interrupt_receipt_v1`, `interrupt_cancel_queued_v1` 등). **버전 문자열 비교 대신 이 배열로 feature-detect 하라고 문서가 권장**한다 (v2.1.205+).
- `system/api_retry` — 재시도 가능한 API 오류 시 emit. 필드: `attempt`, `max_retries`, `retry_delay_ms`, `error_status`, `error`(`rate_limit`, `overloaded`, `billing_error`, `authentication_failed` 등 카테고리), `uuid`, `session_id`. → harness의 rate-limit backoff/알림에 직접 쓸 수 있다.
- subagent 메시지는 `parent_tool_use_id`로 구분된다(main loop은 `null`). 중첩 subagent도 각자 자신을 낳은 Agent tool call ID를 담아 트리 재구성이 가능하다.

#### `-p` 모드의 프로세스 수명 주의점

- Claude가 `-p` 실행 중 시작한 **background Bash task**는 최종 결과 반환 + stdin close 후 약 5초 뒤 종료된다.
- **background subagent / workflow는 이 5초 유예에서 면제**되어 `claude -p`가 완료를 기다린다. v2.1.182부터 이 대기는 기본 10분으로 상한이 걸린다(`CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS`로 조정, `0`이면 무제한).
- consumer가 스트림을 느리게 읽으면 큐가 비워질 때까지 최대 30초 대기 후 종료한다(v2.1.214 이전에는 약 2초라 큰 응답 끝이 잘릴 수 있었음).
- piped stdin은 10MB 상한. 초과 시 non-zero exit.

출처: [Run Claude Code programmatically](https://code.claude.com/docs/en/headless)

---

## 2. Codex

### 2.1 표면 A — Codex Python SDK (`openai-codex`) — **공식 존재**

> 티켓의 "Is there any official Python SDK or is CLI subprocess the only surface?"에 대한 답: **공식 Python SDK가 존재하며 stable release다.**

`pip install openai-codex`, Python 3.10 이상. "The Python SDK controls the local Codex **app-server over JSON-RPC**. Published SDK builds include a pinned Codex CLI runtime dependency." — 즉 별도 Codex CLI 설치·버전 관리 없이 SDK가 자기 런타임을 들고 온다. `pip install --pre openai-codex`로 prerelease 채널 선택 가능. ([Codex SDK](https://learn.chatgpt.com/docs/codex-sdk), [sdk/python README](https://github.com/openai/codex/blob/main/sdk/python/README.md))

동기 `Codex`와 비동기 `AsyncCodex`가 완전 대칭으로 제공된다. dev-crew harness가 asyncio 기반이면 `AsyncCodex`를 쓰면 된다.

`Codex` 주요 메서드: ([sdk/python API reference](https://github.com/openai/codex/blob/main/sdk/python/docs/api-reference.md))

```python
thread_start(*, approval_mode=ApprovalMode.auto_review, base_instructions=None, config=None,
             cwd=None, developer_instructions=None, ephemeral=None, model=None,
             model_provider=None, personality=None, sandbox=None) -> Thread
thread_resume(thread_id, *, approval_mode=..., config=None, cwd=None, model=None, sandbox=None, ...) -> Thread
thread_fork(thread_id, *, ...) -> Thread
thread_archive(thread_id) -> ThreadArchiveResponse
thread_unarchive(thread_id) -> Thread
thread_list(*, archived=None, cursor=None, cwd=None, limit=None,
            model_providers=None, sort_key=None, source_kinds=None) -> ThreadListResponse
models(*, include_hidden=False) -> ModelListResponse
account(*, refresh_token=False) -> GetAccountResponse
login_api_key(api_key) / login_chatgpt() / login_chatgpt_device_code() / logout()
metadata -> InitializeResponse
```

`Thread` 메서드: ([sdk/python API reference](https://github.com/openai/codex/blob/main/sdk/python/docs/api-reference.md))

```python
run(input, *, approval_mode=None, cwd=None, effort=None, model=None,
    output_schema=None, personality=None, sandbox=None, service_tier=None, summary=None) -> TurnResult
turn(input, *, ...same...) -> TurnHandle        # 저수준 turn 제어
read(*, include_turns=False) -> ThreadReadResponse
set_name(name) -> ThreadSetNameResponse
compact() -> ThreadCompactStartResponse
```

`TurnHandle`: `steer(input)`, `interrupt()`, `stream() -> Iterator[Notification]`, `run() -> TurnResult`.

#### 어댑터 연산 매핑 — 6/6 네이티브 지원

| 연산 | 지원 | 방법 |
| --- | --- | --- |
| `startSession` | ✅ | `codex.thread_start(model=..., sandbox=..., cwd=..., approval_mode=..., config={...})` → `Thread` |
| `send` | ✅ | `thread.run(prompt)` 재호출, 또는 `thread.turn(prompt)`로 `TurnHandle`을 받아 `stream()`으로 이벤트 소비 |
| `resume` | ✅ | `codex.thread_resume(thread_id, ...)`. 프로세스 재시작 후에도 동작. 분기는 `codex.thread_fork(thread_id)` |
| `cancel` | ✅ | `TurnHandle.interrupt() -> TurnInterruptResponse`. 취소 대신 **방향 전환**만 원하면 `TurnHandle.steer(input)` |
| `archive` | ✅ | `codex.thread_archive(thread_id)` / `thread_unarchive(thread_id)`. 목록 필터는 `thread_list(archived=...)` |
| `getUsage` | ✅ | `TurnResult.usage: ThreadTokenUsage \| None`. 함께 오는 필드: `id`, `status: TurnStatus`, `error: TurnError \| None`, `started_at`, `completed_at`, `duration_ms`, `final_response`, `items: list[ThreadItem]` |

`duration_ms`가 SDK에서 바로 나오므로 DESIGN.md 6.2의 `getUsage -> token/latency/provider metadata` 중 **latency 부분까지 별도 계측 없이 채워진다.**

#### model / effort 지정

- thread 생성 시: `thread_start(model="gpt-5.6-terra", config={"model_reasoning_effort": "high"})`
- turn 단위 오버라이드: `thread.run(prompt, model=..., effort=...)` — `effort`가 **turn 단위 파라미터로 노출**되어 있어, 같은 thread 안에서 리뷰 깊이를 조절할 수 있다.
- `config=` 딕셔너리는 `config.toml` 키를 그대로 받는다. 공식 예제: `codex.thread_start(model="gpt-5.4", config={"model_reasoning_effort": "high"})`

`model_reasoning_effort` 허용값은 `minimal | low | medium | high | xhigh`이며 "Responses API only; `xhigh` is model-dependent"라고 명시된다. 관련 키: `plan_mode_reasoning_effort`(`none | minimal | low | medium | high | xhigh`), `model_reasoning_summary`(`auto | concise | detailed | none`), `model_verbosity`(`low | medium | high`). ([Configuration Reference](https://learn.chatgpt.com/docs/config-file/config-reference))

#### sandbox / approval

`Sandbox` enum 프리셋 3종 ([Codex SDK](https://learn.chatgpt.com/docs/codex-sdk)):

- `Sandbox.read_only` — 쓰기 없이 읽기만
- `Sandbox.workspace_write` — workspace 및 설정된 writable root 안에서 쓰기 허용
- `Sandbox.full_access` — 파일시스템 제약 없음

`sandbox=`를 생략하면 app-server의 기본값을 쓴다. `run(...)` / `turn(...)`에 넘긴 sandbox는 **해당 turn과 이후 turn에 계속 적용**된다(turn 1회성이 아님 — adapter에서 상태로 추적해야 함). thread 생성 시 `approval_mode`는 기본 `ApprovalMode.auto_review`.

#### 재시도 헬퍼

SDK가 overload 재시도를 내장 제공한다: `retry_on_overload(...)`(exponential backoff + jitter), `is_retryable_error(exc)`, 예외 타입 `JsonRpcError`, `MethodNotFoundError`, `InvalidParamsError`, `ServerBusyError`. ([sdk/python API reference](https://github.com/openai/codex/blob/main/sdk/python/docs/api-reference.md))

---

### 2.2 표면 B — Codex CLI (`codex exec`)

`codex exec` (짧은 형태 `codex e`)는 대화형 TUI 없이 스크립트/CI에서 실행하는 경로다. 실행 중 진행 상황은 **stderr**로 스트리밍되고 **stdout에는 최종 agent 메시지만** 출력된다. ([Non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode))

`codex exec --help` (codex-cli 0.145.0) 기준 주요 옵션:

| 플래그 | 의미 |
| --- | --- |
| `-c, --config <key=value>` | `~/.codex/config.toml` 값 오버라이드. dotted path 지원, value는 TOML로 파싱(실패 시 리터럴 문자열). 예: `-c model="o3"`, `-c model_reasoning_effort="high"` |
| `-m, --model <MODEL>` | 사용할 모델 |
| `-s, --sandbox <MODE>` | `read-only` / `workspace-write` / `danger-full-access` |
| `--json` | stdout을 JSONL 이벤트 스트림으로 전환 |
| `-o, --output-last-message <FILE>` | 최종 메시지를 파일로 기록(stdout 출력도 유지) |
| `--output-schema <FILE>` | 최종 응답 형태를 강제하는 JSON Schema 파일 |
| `--ephemeral` | session rollout 파일을 디스크에 남기지 않음 |
| `-C, --cd <DIR>` | agent의 작업 루트 지정 |
| `--add-dir <DIR>` | 추가 writable 디렉터리 |
| `--skip-git-repo-check` | Git 저장소 밖 실행 허용 |
| `--ignore-user-config` | `$CODEX_HOME/config.toml` 미로드 (auth는 여전히 `CODEX_HOME` 사용) |
| `--ignore-rules` | user/project execpolicy `.rules` 미로드 |
| `--strict-config` | 이 버전이 모르는 config 필드가 있으면 에러 |
| `-p, --profile <NAME>` | `$CODEX_HOME/<name>.config.toml`을 base config 위에 레이어 |
| `--dangerously-bypass-approvals-and-sandbox` | 모든 확인 생략 + 샌드박스 없이 실행 |

`codex exec resume [SESSION_ID] [PROMPT]` 서브커맨드: `SESSION_ID`는 UUID 또는 thread name(UUID 파싱이 우선). `--last`로 최근 세션 선택, `--all`로 cwd 필터 해제. `-m/--model`, `-c`, `--json`, `-o`를 동일하게 받는다.

#### JSONL 이벤트 스키마 (`--json`)

이벤트 타입: `thread.started`, `turn.started`, `turn.completed`, `turn.failed`, `item.*`, `error`. item 타입에는 agent message, reasoning, command execution, file change, MCP tool call, web search, plan update가 포함된다. ([Non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode))

공식 샘플 스트림:

```jsonl
{"type":"thread.started","thread_id":"0199a213-81c0-7800-8aa1-bbab2a035a53"}
{"type":"turn.started"}
{"type":"item.started","item":{"id":"item_1","type":"command_execution","command":"bash -lc ls","status":"in_progress"}}
{"type":"item.completed","item":{"id":"item_3","type":"agent_message","text":"Repo contains docs, sdk, and examples directories."}}
{"type":"turn.completed","usage":{"input_tokens":24763,"cached_input_tokens":24448,"output_tokens":122,"reasoning_output_tokens":0}}
```

→ `thread.started.thread_id`가 세션 ID, `turn.completed.usage`가 토큰 사용량이다. Claude Code와 달리 **USD 추정치는 제공되지 않는다** (토큰 수만). 비용은 harness가 가격표를 들고 직접 환산해야 한다.

#### 어댑터 연산 매핑

| 연산 | 지원 | 방법 |
| --- | --- | --- |
| `startSession` | ✅ | `codex exec --json "<prompt>"` → 첫 줄 `thread.started`의 `thread_id` 회수 |
| `send` | ⚠️ 우회 | 실행 중인 프로세스에 메시지를 주입하는 경로 없음. `codex exec resume <SESSION_ID> "<followup>"`로 **매번 새 프로세스**를 띄워야 한다 |
| `resume` | ✅ | `codex exec resume <SESSION_ID> [PROMPT]` / `--last` / `--all`. 프로세스 재시작 후에도 동작 |
| `cancel` | ⚠️ 우회 | 전용 플래그 없음. 프로세스에 SIGINT/SIGTERM 전송 |
| `archive` | ✅ | `codex archive <SESSION>` / `codex unarchive <SESSION>` — "session picker를 정리하되 transcript는 삭제하지 않는" 용도. Session ID가 session name보다 우선. 영구 삭제는 `codex delete <SESSION>` (`--force`는 UUID에만 허용) |
| `getUsage` | ✅ | `--json` 스트림의 `turn.completed.usage` (`input_tokens`, `cached_input_tokens`, `output_tokens`, `reasoning_output_tokens`) |

#### 그 밖의 프로토콜 표면 (참고)

- `codex app-server` — local Codex app server. JSONL-over-stdio(`--listen stdio://`, 별칭 `--stdio`), WebSocket(`--listen ws://IP:PORT`), Unix socket(`--listen unix://[PATH]`) 전송 지원. 다만 문서에 "primarily for development and debugging and **may change without notice**"로 명시 — Python SDK가 이 위에 올라가 있으므로, harness는 app-server를 직접 말하지 말고 SDK를 쓰는 것이 맞다.
- `codex mcp-server` — Codex를 MCP 서버로 노출(stdio). Codex를 더 넓은 오케스트레이션 안의 한 전문가로 쓰는 경우 공식 문서가 권장하는 경로. ([Codex SDK](https://learn.chatgpt.com/docs/codex-sdk))

출처: [Non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode), [Command line options](https://learn.chatgpt.com/docs/developer-commands?surface=cli), 로컬 `codex exec --help` / `codex exec resume --help` (codex-cli 0.145.0)

---

## 3. 제약 조건

### 3.1 인증

| | Claude Code | Codex |
| --- | --- | --- |
| 구독 로그인 | `/login` (Claude Pro / Max / Team / Enterprise, Claude Console) | `codex login` (ChatGPT OAuth 브라우저 플로우, 기본값) |
| API key | `ANTHROPIC_API_KEY` (`X-Api-Key` 헤더) | `CODEX_API_KEY` — **`codex exec`에서만 지원** / `codex login --with-api-key` (stdin) |
| Bearer token | `ANTHROPIC_AUTH_TOKEN` (`Authorization: Bearer`, LLM gateway용) | `CODEX_ACCESS_TOKEN` / `codex login --with-access-token` |
| 장기 CI 토큰 | `claude setup-token` → 1년짜리 OAuth 토큰을 `CLAUDE_CODE_OAUTH_TOKEN`에 설정 | `~/.codex/auth.json` 시딩 (보안 스토리지 경유, 런 사이 유지) |
| 동적 자격증명 | `apiKeyHelper` 설정 (기본 5분 또는 HTTP 401마다 재호출, `CLAUDE_CODE_API_KEY_HELPER_TTL_MS`로 조정) | model provider별 `env_key` config |
| 자격증명 저장 위치 | macOS Keychain / Linux `~/.claude/.credentials.json` (mode 0600) / Windows `%USERPROFILE%\.claude\.credentials.json` | `~/.codex/auth.json` 평문 또는 OS credential store (`auto` 모드) |
| 헤드리스 로그인 확인 | `/status` | `codex login status` — 자격증명 존재 시 exit 0 (자동화 스크립트용) |

**Claude Code 자격증명 우선순위** (문서 명시 순서): 1) cloud provider(`CLAUDE_CODE_USE_BEDROCK` / `_VERTEX` / `_FOUNDRY`) → 2) `ANTHROPIC_AUTH_TOKEN` → 3) `ANTHROPIC_API_KEY` → 4) `apiKeyHelper` → 5) `CLAUDE_CODE_OAUTH_TOKEN` → 6) Anthropic profile / federation → 7) `/login` 구독 OAuth. 비대화형(`-p`)에서는 `ANTHROPIC_API_KEY`가 있으면 **항상** 사용된다(대화형과 달리 승인 프롬프트 없음). ([Authentication](https://code.claude.com/docs/en/authentication))

**harness에서 반드시 알아야 할 함정 2개:**

1. `claude --bare`는 OAuth credential과 시스템 keychain을 **읽지 않으며**, `CLAUDE_CODE_OAUTH_TOKEN`도 읽지 않는다. `--bare`를 쓰려면 `ANTHROPIC_API_KEY` 또는 `--settings` JSON의 `apiKeyHelper`가 필요하다. ([headless](https://code.claude.com/docs/en/headless), [Authentication](https://code.claude.com/docs/en/authentication))
2. `CODEX_API_KEY` / `OPENAI_API_KEY`를 job 레벨 환경변수로 두지 말라고 공식 문서가 명시적으로 경고한다 — 같은 job에서 도는 build script, test, dependency lifecycle hook, 침해된 action이 읽을 수 있기 때문. **단일 `codex exec` 호출에만 inline으로 설정**하라. ([Non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode))

**배포 관련 정책 제약 (Claude Agent SDK):** quickstart 문서가 명시한다 — "Unless previously approved, Anthropic does not allow third party developers to offer claude.ai login or rate limits for their products, **including agents built on the Claude Agent SDK**. Please use the API key authentication methods described in this document instead." ([quickstart](https://code.claude.com/docs/en/agent-sdk/quickstart))

dev-crew를 운영자 본인이 자기 자격증명으로 돌리는 내부 harness로 유지하는 한 이 조항의 대상이 아니다. 그러나 harness를 제품으로 배포하면서 사용자에게 claude.ai 로그인을 태우는 순간 사전 승인이 필요해진다. **로드맵 결정 사항으로 기록해 둘 것.**

과금 모델 차이: Codex는 API key로 로그인하면 ChatGPT 플랜 크레딧이 아니라 **표준 API 요금**으로 청구된다. ([Authentication](https://learn.chatgpt.com/docs/auth))

### 3.2 동시 세션

| | Claude Code | Codex |
| --- | --- | --- |
| 명시적 상한 | 문서화된 프로세스 수 상한 없음 | 문서화된 상한 없음 |
| 프로세스 모델 | 세션 1개 = CLI 프로세스 1개 (SDK도 CLI를 감쌈) | Python SDK: `Codex` 인스턴스 1개가 **여러 활성 turn을 동시에 스트리밍 가능** — turn stream이 turn ID로 라우팅됨 |
| 격리 권장 방식 | worktree — 문서가 "run isolated parallel sessions on separate branches"로 명시 | `-C/--cd` + `--add-dir`로 workspace 지정, sandbox 프리셋으로 쓰기 범위 제한 |

Codex Python SDK가 "one `Codex` instance can stream multiple active turns concurrently"를 **명시적으로 보장**하는 것이 실질적 차이다. Claude Code는 동시성을 프로세스 수로 사는 반면, Codex는 app-server 하나에 다중 thread를 물릴 수 있어 Reviewer fan-out 시 리소스 프로파일이 유리하다. ([sdk/python API reference](https://github.com/openai/codex/blob/main/sdk/python/docs/api-reference.md), [Manage sessions](https://code.claude.com/docs/en/sessions))

동시성의 실질 상한은 프로세스 수가 아니라 **rate limit**이다(§3.3).

### 3.3 Rate limit

**Claude Code (Console/API 조직)** — 공식 문서의 팀 규모별 per-user 권장치: ([Manage costs](https://code.claude.com/docs/en/costs))

| 팀 규모 | TPM/user | RPM/user |
| --- | --- | --- |
| 1–5 | 200k–300k | 5–7 |
| 5–20 | 100k–150k | 2.5–3.5 |
| 20–50 | 50k–75k | 1.25–1.75 |
| 50–100 | 25k–35k | 0.62–0.87 |
| 100–500 | 15k–20k | 0.37–0.47 |
| 500+ | 10k–15k | 0.25–0.35 |

이 한도는 **개인이 아니라 조직 레벨**에 적용되므로, 다른 사용자가 놀고 있으면 한 사용자가 자기 몫 이상을 일시적으로 쓸 수 있다. dev-crew처럼 단일 사용자가 다수 인스턴스를 병렬로 돌리는 워크로드는 이 특성에 기댈 수 있지만, 동시에 전체 조직 한도를 혼자 소진할 수도 있다. Claude Code 트래픽은 자동 생성되는 "Claude Code" workspace에 집계되며, 해당 workspace에 별도 rate limit을 걸어 다른 프로덕션 워크로드를 보호할 수 있다.

**Claude Code (구독)** — Team/Enterprise는 seat tier(Standard/Premium)별 per-seat allowance가 **rolling 5시간 창 + 주간 창**으로 리셋되며, 이 allowance는 Claude chat 및 Cowork과 공유된다. Pro/Max는 usage credit으로 한도 초과 지속이 가능하다.

**Codex** — 단일 숫자 표로 공표된 rate limit 문서는 없다. 공식 문서상 두 축:
- ChatGPT 로그인 시 → ChatGPT 워크스페이스의 usage limit / spend control이 적용된다. 다만 문서 자체가 "These controls aren't a universal Codex limit system"이라고 단서를 단다. ([ChatGPT usage limits and spend controls](https://learn.chatgpt.com/docs/enterprise/usage-limits))
- API key 로그인 시 → OpenAI Platform 조직의 표준 API rate limit과 요금이 적용된다. ([Authentication](https://learn.chatgpt.com/docs/auth))

**429 감지 경로:** Claude Code는 `stream-json`의 `system/api_retry` 이벤트에서 `error: "rate_limit"` 카테고리와 `retry_delay_ms`를 직접 준다(§1.2). Codex Python SDK는 `ServerBusyError` / `is_retryable_error()` / `retry_on_overload()`를 제공한다. 두 경로 모두 harness의 backoff에 그대로 연결 가능하다.

### 3.4 사용량 메타데이터 노출 비교

| | Claude Code | Codex |
| --- | --- | --- |
| 토큰 필드 | `input_tokens`, `output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens` | `input_tokens`, `cached_input_tokens`, `output_tokens`, `reasoning_output_tokens` |
| USD 추정 | ✅ `total_cost_usd` (**client-side 추정치**, 청구 근거로 쓰지 말 것) | ❌ 없음. 토큰 수만 제공 |
| 모델별 분해 | ✅ `model_usage` (subagent 포함) | ❌ (`turn.completed.usage`는 단일 집계) |
| latency | 직접 필드 없음 (harness가 계측) | ✅ `TurnResult.duration_ms`, `started_at`, `completed_at` |
| reasoning 토큰 분리 | ❌ (output에 합산) | ✅ `reasoning_output_tokens` 별도 |
| 권위 있는 청구 데이터 | [Usage and Cost API](https://platform.claude.com/docs/en/build-with-claude/usage-cost-api), Claude Console Usage 페이지 | OpenAI Platform 계정 |

→ harness의 usage 스키마는 **두 provider의 합집합**으로 잡되, `total_cost_usd`(Claude 전용)와 `reasoning_output_tokens`/`duration_ms`(Codex 전용)를 nullable로 두고 raw payload를 그대로 보존해야 한다. DESIGN.md 6.2가 "provider 고유 ID와 raw usage metadata를 보존한다"고 정한 것과 일치한다.

### 3.5 세션 영속성 / 보관

| | Claude Code | Codex |
| --- | --- | --- |
| 저장 위치 | `~/.claude/projects/<encoded-cwd>/<session-id>.jsonl` (`CLAUDE_CONFIG_DIR`로 이동 가능) | `$CODEX_HOME`(기본 `~/.codex`) 하위. SQLite 상태는 `CODEX_SQLITE_HOME` |
| encoded-cwd 규칙 | 절대 경로의 non-alphanumeric 문자를 전부 `-`로 치환. 변환명이 200자 초과 시 200자로 자르고 전체 경로 해시를 덧붙임 | — |
| 기본 보존 기간 | **30일** (`cleanupPeriodDays` 설정으로 변경) | 문서상 자동 만료 기간 명시 없음 |
| 영속화 끄기 | `--no-session-persistence` (단발) / `CLAUDE_CODE_SKIP_PROMPT_HISTORY` (전역) | `--ephemeral` (CLI) / `thread_start(ephemeral=True)` (SDK) |
| 크로스 호스트 resume | 세션 파일이 **같은 머신에 있어야 함**. 대안: `SessionStore` 어댑터로 외부 백엔드 미러링(`ClaudeAgentOptions.session_store`, `session_store_flush="batched"\|"eager"`), 또는 jsonl 파일을 옮기기 | 문서상 별도 크로스 호스트 메커니즘 없음 (`auth.json` 시딩은 인증용) |

**주의: 30일 기본 보존 기간은 harness가 장기 세션 registry를 신뢰할 수 없다는 뜻이다.** DESIGN.md 6.3의 "resume이 불가능하면 `FAILED_RECOVERY`로 종료하고 요약 artifact를 전달한 새 instance를 생성한다" 규칙은 **필수 경로**지 예외 경로가 아니다.

출처: [Manage sessions](https://code.claude.com/docs/en/sessions), [Work with sessions](https://code.claude.com/docs/en/agent-sdk/sessions), [Environment variables](https://learn.chatgpt.com/docs/config-file/environment-variables), [Non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode)

---

## 4. dev-crew 어댑터 설계에 대한 함의

### 4.1 표면 선택 권고

| Adapter | 권장 표면 | 근거 |
| --- | --- | --- |
| Claude Code Adapter | **Agent SDK for Python — `ClaudeSDKClient`** | 6개 연산 중 5개가 네이티브. `interrupt()`가 `query()`에서는 안 되고 `ClaudeSDKClient`에서만 되므로 클라이언트 경로가 강제된다. subprocess 파싱보다 타입 있는 message 객체가 유지보수에 유리 |
| Codex Adapter | **Codex Python SDK — `AsyncCodex`** | 6개 연산 **전부** 네이티브. CLI subprocess 대비 `send`와 `cancel`이 우회 없이 해결되고, 단일 인스턴스 다중 turn 동시성 보장 + `duration_ms` 무상 제공 |
| 폴백 | 두 CLI 모두 유지 | CLI는 SDK가 못 하는 디버깅/재현 경로로 남긴다. Codex `app-server`를 직접 말하는 것은 "may change without notice" 경고 때문에 비권장 |

### 4.2 `effortLevel` 매핑 (비대칭 주의)

DESIGN.md 6.1의 공통 표현 `LOW | MEDIUM | HIGH | MAX`:

| 공통 | Claude Code (`--effort` / `options.effort`) | Codex (`model_reasoning_effort` / `run(effort=)`) |
| --- | --- | --- |
| `LOW` | `low` | `low` |
| `MEDIUM` | `medium` | `medium` |
| `HIGH` | `high` | `high` |
| `MAX` | `max` | ❌ **없음** — 최상위는 `xhigh`이고 그마저 model-dependent |
| (해당 없음) | `xhigh`, `ultracode`(CLI 전용) | `minimal` |

`MAX`를 Codex 인스턴스에 라우팅하면 매핑할 값이 없다. DESIGN.md 6.1이 정한 "지원하지 않는 조합은 Adapter가 조용히 변경하지 않고 **routing validation error로 반환**"이 적용되어야 하는 첫 번째 실사례다. Reviewer가 Codex 기본이므로(DESIGN.md 5), Reviewer routing policy에서 `MAX`를 허용할지 여부를 먼저 결정해야 한다.

### 4.3 `archive` 연산의 provider 간 비대칭

Codex는 `thread_archive` / `thread_unarchive`가 있고 `thread_list(archived=...)`로 조회까지 되지만, Claude Code에는 대응물이 없다. 공통 계약을 유지하려면 Claude Code Adapter에서 archive를 **harness 측 상태(AgentInstance.status = `ARCHIVED`) + `tag_session()` 태깅 + transcript 파일 보관**의 합성으로 구현하고, 이것이 provider 네이티브가 아님을 어댑터 문서에 명시해야 한다.

### 4.4 취소 경로

| | 진행 중 turn 중단 | 프로세스 종료 |
| --- | --- | --- |
| Claude Code | `client.interrupt()` (streaming 전용). `system/init.capabilities`에 `interrupt_receipt_v1` / `interrupt_cancel_queued_v1`이 있는지로 feature-detect | SIGTERM → turn abort + Bash process tree 종료 + `SessionEnd` hook 실행 + exit 143 |
| Codex | `TurnHandle.interrupt()` | SIGINT/SIGTERM |

Codex는 취소 외에 `TurnHandle.steer(input)`으로 **진행 중 turn의 방향만 바꾸는** 중간 옵션을 제공한다. DESIGN.md의 Cancel 신호(§7.x, line 655)와 별개의 상태로 모델링할 가치가 있다.

---

## 5. 미해결 / 후속 확인 필요

- Codex Python SDK의 `ThreadTokenUsage` 정확한 필드 구성은 API reference에 타입명만 나오고 필드가 열거되어 있지 않다. `--json` 스트림의 `turn.completed.usage`와 동일 형태(`input_tokens`, `cached_input_tokens`, `output_tokens`, `reasoning_output_tokens`)로 추정되나 **런타임 검증 필요**.
- Codex의 구체적 rate limit 수치(RPM/TPM)는 공표된 1차 문서가 없다. 실측 또는 OpenAI Platform 조직 설정 확인 필요.
- Claude Code `SessionStore` 어댑터(`ClaudeAgentOptions.session_store`)로 세션을 외부 백엔드에 미러링하면 크로스 호스트 resume이 가능하다. dev-crew가 다중 호스트로 확장될 경우 별도 조사 대상.

---

## 출처

### Claude Code
- [Agent SDK reference — Python](https://code.claude.com/docs/en/agent-sdk/python)
- [Agent SDK quickstart](https://code.claude.com/docs/en/agent-sdk/quickstart)
- [Work with sessions (Agent SDK)](https://code.claude.com/docs/en/agent-sdk/sessions)
- [Track cost and usage (Agent SDK)](https://code.claude.com/docs/en/agent-sdk/cost-tracking)
- [Streaming Input (Agent SDK)](https://code.claude.com/docs/en/agent-sdk/streaming-vs-single-mode)
- [Persist sessions to external storage](https://code.claude.com/docs/en/agent-sdk/session-storage)
- [CLI reference](https://code.claude.com/docs/en/cli-reference)
- [Run Claude Code programmatically (headless)](https://code.claude.com/docs/en/headless)
- [Manage sessions](https://code.claude.com/docs/en/sessions)
- [Authentication](https://code.claude.com/docs/en/authentication)
- [Manage costs effectively](https://code.claude.com/docs/en/costs)
- 로컬 `claude --help` (v2.1.232)

### Codex
- [Codex SDK](https://learn.chatgpt.com/docs/codex-sdk)
- [openai/codex — sdk/python README](https://github.com/openai/codex/blob/main/sdk/python/README.md)
- [openai/codex — sdk/python API reference](https://github.com/openai/codex/blob/main/sdk/python/docs/api-reference.md)
- [Non-interactive mode (`codex exec`)](https://learn.chatgpt.com/docs/non-interactive-mode)
- [Command line options](https://learn.chatgpt.com/docs/developer-commands?surface=cli)
- [Configuration Reference (`config.toml`)](https://learn.chatgpt.com/docs/config-file/config-reference)
- [Environment variables](https://learn.chatgpt.com/docs/config-file/environment-variables)
- [Authentication](https://learn.chatgpt.com/docs/auth)
- [ChatGPT usage limits and spend controls](https://learn.chatgpt.com/docs/enterprise/usage-limits)
- 로컬 `codex exec --help`, `codex exec resume --help` (codex-cli 0.145.0)
