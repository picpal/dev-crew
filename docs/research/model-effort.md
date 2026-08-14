# provider별 유효 model ID와 effort/reasoning 값 조사

- 티켓: [#3](https://github.com/picpal/dev-crew/issues/3)
- 조사일: 2026-08-14
- 조사 범위: Claude Code CLI, Codex CLI
- 출처 정책: 공식 1차 문서(`code.claude.com/docs`, `platform.claude.com/docs`, `learn.chatgpt.com/docs`, `developers.openai.com/api/docs`)와 로컬에 설치된 CLI 바이너리 자체만 사용. 블로그·커뮤니티·3rd party 요약은 근거로 쓰지 않음.
- 결론 소유권: 이 문서는 **조사 결과**다. tier → model ID 확정은 티켓 #12의 결정 사항이며, 아래 매핑은 모두 **candidate**로 표기했다.

---

## 0. 요약 (결론 먼저)

| 항목 | 결과 |
|---|---|
| Claude Code effort 실제 값 | `low` / `medium` / `high` / `xhigh` / `max` (+ `ultracode`는 effort가 아님) |
| Codex effort 실제 값 | `none` / `low` / `medium` / `high` / `xhigh` / `max` (GPT-5.6 계열 기준, 기본 `medium`) |
| DESIGN.md §6.1 `effortLevel` | `LOW` / `MEDIUM` / `HIGH` / `MAX` — **4단계** |
| 핵심 gap 1 | 양 provider 모두 실제로는 5단계다. 4단계 모델은 `xhigh`를 표현할 수 없는데, `xhigh`는 **양쪽 문서 모두 coding/agentic 작업의 권장값**이다. |
| 핵심 gap 2 | Claude Haiku 4.5는 **effort를 지원하지 않는다**. CHEAP tier를 Haiku로 두면 `effortLevel` 조합 자체가 존재하지 않는다. |
| 핵심 gap 3 | Claude Code는 미지원 effort를 **조용히 하향**한다. DESIGN.md §6.1의 "조용히 변경하지 않고 routing validation error" 요구와 정면 충돌한다. Adapter가 호출 **전에** 자체 검증해야 한다. |
| 핵심 gap 4 | Codex CLI에는 `--reasoning-effort` 플래그가 **없다**. `-c model_reasoning_effort=<v>` 또는 profile만 가능. |

---

## 1. Claude Code

### 1.1 `--model`로 지정 가능한 값

Claude Code의 `model` 설정에는 **model alias** 또는 **full model name**을 넣을 수 있다.
출처: <https://code.claude.com/docs/en/model-config>

#### alias 표 (1차 문서 원문 기준)

| alias | 동작 |
|---|---|
| `default` | model override를 해제하고 계정 유형의 권장 모델(또는 organization default model)로 되돌림. **alias가 아닌 특수값** |
| `best` | 조직이 접근 가능하면 Fable 5, 아니면 최신 Opus |
| `fable` | Claude Fable 5 |
| `sonnet` | 최신 Sonnet |
| `opus` | 최신 Opus |
| `haiku` | Haiku |
| `sonnet[1m]` | 1M context window의 Sonnet |
| `opus[1m]` | 1M context window의 Opus |
| `opusplan` | plan mode에서는 `opus`, 실행 단계에서는 `sonnet`으로 전환하는 특수 모드 |

alias 해석 결과는 provider에 따라 달라진다.

| Provider | `opus` | `sonnet` |
|---|---|---|
| Anthropic API | Opus 5 | Sonnet 5 |
| Claude Platform on AWS | Opus 5 | Sonnet 4.6 |
| Amazon Bedrock, Google Cloud's Agent Platform | Opus 5 | Sonnet 4.5 |
| Microsoft Foundry | Opus 4.6 | Sonnet 4.5 |

> **harness 관점 판단**: alias는 시간이 지나면 가리키는 버전이 바뀌고 provider마다 다르게 풀린다. dev-crew처럼 `ModelRoutingEvent`로 재현성을 요구하는 harness는 **alias가 아니라 full model ID를 pin** 해야 한다. 문서도 "To pin to a specific version, use the full model name, for example `claude-opus-5`"라고 명시한다.

#### full model ID / 비용 tier

출처: <https://platform.claude.com/docs/en/about-claude/models/overview>

| 모델 | Claude API ID | Input $/MTok | Output $/MTok | Context | Max output | effort 지원 |
|---|---|---:|---:|---|---|---|
| Claude Fable 5 | `claude-fable-5` | 10 | 50 | 1M | 128k | O |
| Claude Opus 5 | `claude-opus-5` | 5 | 25 | 1M | 128k | O |
| Claude Sonnet 5 | `claude-sonnet-5` | 2 | 10 | 1M | 128k | O |
| Claude Haiku 4.5 | `claude-haiku-4-5` (ID: `claude-haiku-4-5-20251001`) | 1 | 5 | 200k | 64k | **X** |
| Claude Opus 4.8 (legacy) | `claude-opus-4-8` | 5 | 25 | 1M | 128k | O |
| Claude Opus 4.7 (legacy) | `claude-opus-4-7` | 5 | 25 | 1M | 128k | O |
| Claude Opus 4.6 (legacy) | `claude-opus-4-6` | 5 | 25 | 1M | 128k | O (xhigh 불가) |
| Claude Sonnet 4.6 (legacy) | `claude-sonnet-4-6` | 3 | 15 | 1M | 128k | O (xhigh 불가) |

- 상대 비용 tier: **Fable 5 (10/50) > Opus 5 · 4.8 · 4.7 · 4.6 (5/25) > Sonnet 4.6 (3/15) > Sonnet 5 (2/10) > Haiku 4.5 (1/5)**.
- Sonnet 5가 Sonnet 4.6보다 싸다는 점은 직관과 어긋나므로 routing policy에서 legacy Sonnet 4.6을 선택할 이유가 사실상 없다.
- Claude API의 모든 model ID는 pinned snapshot이다. 4.6 세대부터는 날짜 없는 형식도 evergreen pointer가 아니라 pinned snapshot이다.
- Fable 5는 zero data retention 환경에서 사용할 수 없고, 플랜/시트 등급에 따라 usage credit으로 과금될 수 있다(비대화형 `-p` 모드에서는 동의 프롬프트 없이 과금).

#### 버전 요구사항 (harness preflight 대상)

- Opus 5: Claude Code v2.1.219 이상
- Sonnet 5: v2.1.197 이상
- Opus 4.8: v2.1.154 이상
- Fable 5: v2.1.170 이상
- `--effort ultracode`: v2.1.203 이상

### 1.2 effort/reasoning 제어 메커니즘

출처: <https://code.claude.com/docs/en/model-config> (§Adjust effort level, §Extended thinking)

#### 모델별 지원 레벨 (원문 표)

| 모델 | 지원 레벨 |
|---|---|
| Fable 5 | `low`, `medium`, `high`, `xhigh`, `max` |
| Opus 5, Sonnet 5, Opus 4.8, Opus 4.7 | `low`, `medium`, `high`, `xhigh`, `max` |
| Opus 4.6, Sonnet 4.6 | `low`, `medium`, `high`, `max` |
| **위 표에 없는 모델 (= Haiku 4.5 포함)** | **effort 미지원** |

기본값: effort를 지원하는 모든 모델에서 `high`. 예외적으로 Opus 4.7만 `xhigh`.

#### 각 레벨의 의미 (원문 표)

| Level | 용도 |
|---|---|
| `low` | 짧고 범위가 좁고 지연에 민감한, 지능 민감도가 낮은 작업 |
| `medium` | 일부 지능을 희생해 토큰을 줄이는 비용 민감 작업 |
| `high` | 토큰과 지능의 균형. Opus 4.7을 제외한 모든 모델의 기본값 |
| `xhigh` | 더 높은 토큰 소비로 더 깊은 추론. Opus 4.7의 기본값 |
| `max` | 어려운 작업의 성능을 올릴 수 있으나 수익 체감이 있고 overthinking 경향. 광범위 적용 전 검증 필요 |
| `ultracode` | **model effort level이 아니라 Claude Code 설정.** `xhigh`를 모델에 보내고 추가로 dynamic workflow를 오케스트레이션. 세션 한정 |

> 문서 명시: "The effort scale is calibrated per model, so the same level name does not represent the same underlying value across models." — 즉 provider 내부에서도 동일 이름이 동일 강도를 뜻하지 않는다. dev-crew의 `reasoningLevel`(실제 적용 정규화값)을 별도 기록하는 설계는 이 점에서 타당하다.

#### 설정 경로와 우선순위

문서가 명시한 우선순위: **환경변수 > 설정 레벨 > 모델 기본값**. frontmatter effort는 해당 skill/subagent가 활성일 때 세션 레벨을 덮지만 환경변수는 못 덮는다.

| 경로 | 형태 | 제약 |
|---|---|---|
| `/effort` 슬래시 커맨드 | `/effort <level>`, 인자 없으면 슬라이더, `/effort auto`로 모델 기본값 복귀 | 비대화형(`-p`)에서는 세션 한정, default로 저장 안 됨 |
| `/model` 내부 슬라이더 | 좌우 화살표로 조정 | 대화형 전용 |
| `--effort` 플래그 | `claude --effort <level>` | 해당 세션 한정 |
| `CLAUDE_CODE_EFFORT_LEVEL` 환경변수 | 레벨명 | **모든 방법보다 우선.** `ultracode` 미허용 |
| settings 파일 `effortLevel` | `low`/`medium`/`high`/`xhigh` | **`max`와 `ultracode`는 허용 안 됨(세션 한정)** |
| skill / subagent frontmatter `effort` | 레벨명 | 해당 skill·subagent 실행 시에만 |

#### thinking / reasoning 관련 추가 제어

- **적응형 추론(adaptive reasoning)**: Fable 5, Sonnet 5, Opus 4.7 이상은 **항상** 적응형이다. 고정 thinking budget 모드와 `CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING`은 이들에 적용되지 않는다.
- Opus 4.6 / Sonnet 4.6에서만 `CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING=1`로 `MAX_THINKING_TOKENS` 기반 고정 budget으로 되돌릴 수 있다.
- `MAX_THINKING_TOKENS=0`은 Anthropic API에서 thinking을 끈다. 단 **Fable 5에서는 thinking을 끌 수 없다** (세션 토글, `alwaysThinkingEnabled`, `MAX_THINKING_TOKENS=0` 모두 무효).
- 프롬프트에 `ultrathink`를 포함하면 해당 턴만 더 깊은 추론을 요청한다. **API로 전송되는 effort level은 바뀌지 않는다.** 즉 관측 지표(`reasoningLevel`)에 나타나지 않는 side channel이므로 harness가 이 방식에 의존하면 안 된다.
- `think`, `think hard`, `think more` 등 다른 표현은 키워드로 인식되지 않고 일반 프롬프트 텍스트로 전달된다.

#### 조직 차원 제약 (multi-tenant 배포 시)

- `availableModels` / `enforceAvailableModels` (managed settings): 선택 가능한 모델을 제한. 차단된 선택은 위치에 따라 거부되거나 대체된다.
- **Organization effort limits**: Enterprise 관리자가 모델별·역할별 effort 상한을 걸 수 있다. 상한보다 높은 레벨을 `--effort`/`/effort`로 지정하면 **상한값으로 실행**된다. json/stream-json 출력이나 background agent에서는 **경고 없이 조용히 clamp**된다. (v2.1.195 이상)

> **routing validation 영향**: 조직 상한과 모델 미지원 하향이 겹치면, harness가 요청한 effort와 실제 적용 effort가 달라질 수 있고 그 사실이 stdout에 나타나지 않을 수 있다. `ModelRoutingEvent.reasoningLevel`을 "요청값"이 아니라 **provider가 실제 적용한 값**으로 채우려면 별도 확인 경로가 필요하다(→ 후속 티켓 후보).

### 1.3 subagent / teammate 모델 지정

dev-crew는 다중 agent harness이므로 관련 surface를 함께 정리한다. 모두 `availableModels` allowlist의 적용 대상이다.

- subagent frontmatter의 `model` 필드
- Agent tool의 `model` 파라미터
- agent team teammate 모델 및 `teammateDefaultModel` 설정
- `CLAUDE_CODE_SUBAGENT_MODEL` 환경변수
- skill / command frontmatter의 `model`
- advisor: `advisorModel` 설정 및 `--advisor` 플래그

차단 시 동작이 surface마다 다르다. subagent/teammate override가 차단되면 **실패가 아니라 상속 모델로 fallback**하고, teammate의 fallback은 사용자에게 보고되지 않는다. skill/command override가 차단되면 **override를 무시하고 세션 모델로 실행**한다. → harness가 subagent 모델을 지정했다고 해서 그 모델로 돌았다고 가정하면 안 된다.

---

## 2. Codex CLI

검증 환경: 로컬 설치본 `codex-cli 0.145.0` (`/opt/homebrew/bin/codex`).
출처: <https://learn.chatgpt.com/docs/config-file/config-reference>, <https://learn.chatgpt.com/docs/config-file/config-basic>, <https://learn.chatgpt.com/docs/models>, <https://developers.openai.com/api/docs/models/gpt-5.6-sol> (및 terra/luna), 그리고 설치된 CLI 바이너리의 `--help` 출력과 내장 문자열.

### 2.1 유효 model ID와 비용 tier

출처: <https://learn.chatgpt.com/docs/models>, <https://developers.openai.com/api/docs/models/>

| 모델 | slug | 위치 | Input $/1M | Output $/1M |
|---|---|---|---:|---:|
| GPT-5.6 Sol | `gpt-5.6-sol` | "Flagship GPT-5.6 model with the strongest capability for complex coding, computer use, research, and cybersecurity." Premium tier | 5 | 30 |
| GPT-5.6 Terra | `gpt-5.6-terra` | "Balanced GPT-5.6 model for everyday work, with performance competitive with GPT-5.5 at a lower cost." Mid-tier | 2 | 12 |
| GPT-5.6 Luna | `gpt-5.6-luna` | "Fast and affordable GPT-5.6 model that delivers strong capability at the lowest cost in the family." Budget tier | 0.2 | 1.2 |
| GPT-5.3 Codex Spark | (research preview) | "Text-only research preview model optimized for near-instant, real-time coding iteration." ChatGPT Pro 한정 | — | — |
| GPT-5.5 | `gpt-5.5` | 이전 세대 frontier | — | — |
| GPT-5.4 / GPT-5.4 Mini | — | **2026-08-31 은퇴 예정.** 각각 Terra / Luna로 대체 권고 | — | — |

- 상대 비용 tier: **Sol (5/30) > Terra (2/12) > Luna (0.2/1.2)**.
- GPT-5.4 계열은 이 조사 시점(2026-08-14) 기준 **17일 뒤 은퇴**한다. 신규 매핑에 넣으면 안 된다.

### 2.2 reasoning effort — 정확한 허용값

이 항목은 문서 간 불일치가 있어 **설치된 CLI 바이너리를 직접 검증**했다.

**(a) 공식 config reference** — <https://learn.chatgpt.com/docs/config-file/config-reference>

> `model_reasoning_effort` — Type: `string`, Accepted values: `minimal | low | medium | high | xhigh`.
> "Adjust reasoning effort for supported models (Responses API only; `xhigh` is model-dependent)."

**(b) 모델 API 문서** — `gpt-5.6-sol` / `gpt-5.6-terra` / `gpt-5.6-luna` 페이지 모두 동일:

> reasoning.effort supports "none, low, medium (default), high, xhigh, and max."

**(c) 설치된 CLI 바이너리(0.145.0) 내장 문자열** — serde enum 직렬화 목록:

```
minimal low medium high xhigh max ultra
```

그리고 바이너리에 내장된 subagent 도구 설명문(원문 그대로):

> `GPT-5.6 supports \`none\`, \`low\`, \`medium\`, \`high\`, \`xhigh\`, and \`max\`. If omitted, GPT-5.6 defaults to \`medium\`.`

**정리 — 현재 사실관계:**

| 값 | CLI가 파싱하는가 | GPT-5.6 계열에서 유효한가 | 비고 |
|---|---|---|---|
| `none` | O | O | 추론 사실상 비활성. Claude Code에 대응값 없음 |
| `minimal` | O | **X** (GPT-5 세대 레거시) | config reference에는 있으나 GPT-5.6 지원 목록에는 없음 |
| `low` | O | O | |
| `medium` | O | O (**기본값**) | |
| `high` | O | O | |
| `xhigh` | O | O | "model-dependent"로 명시 |
| `max` | O | O | **config reference 문서에 누락됨** (문서 lag) |
| `ultra` | O | 별도 | effort 값이 아니라 **proactive multi-agent(멀티 에이전트 위임) 동작**. 바이너리 문자열: `Use \`effort: "ultra"\` for proactive multi-agent behavior.` |

> **주의**: config reference 문서의 허용값 목록(`minimal|low|medium|high|xhigh`)은 **낡았다**. `max`가 빠져 있고 GPT-5.6에 없는 `minimal`이 남아 있다. harness는 문서 목록이 아니라 **모델별 지원 목록**을 기준으로 검증해야 한다.

### 2.3 전달 방식 — config.toml vs CLI 플래그

**결론: reasoning effort 전용 CLI 플래그는 존재하지 않는다.**

`codex --help`(0.145.0) 전체 옵션 목록을 확인한 결과 `--reasoning-effort` 류의 플래그는 없다. 사용 가능한 관련 경로는 다음 세 가지다.

| 방식 | 형태 | 확인 근거 |
|---|---|---|
| config.toml | `model_reasoning_effort = "high"` | config reference / config basic |
| CLI config override | `-c model_reasoning_effort=high` (`--config`의 축약) | `codex --help`: "Override a configuration value that would otherwise be loaded from `~/.codex/config.toml`. Use a dotted path (`foo.bar.baz`) to override nested values. The `value` portion is parsed as TOML." |
| profile | `-p, --profile <name>` → `$CODEX_HOME/<name>.config.toml`을 base user config 위에 레이어링 | `codex --help` |

model은 전용 플래그가 있다: `-m, --model <MODEL>` ("Model the agent should use"). `codex`, `codex exec`, `codex review` 모두에서 사용 가능.

우선순위(config basic 기준): **CLI 플래그 > 프로젝트 config(`.codex/config.toml`) > profile 파일 > 사용자 config(`~/.codex/config.toml`) > 시스템 기본값**.

관련 추가 키:

- `plan_mode_reasoning_effort` — plan mode 전용 effort (바이너리 내장 키. dev-crew의 ARCHITECT role과 관련될 수 있음)
- `model_reasoning_summary` — `auto | concise | detailed | none`
- `model_verbosity` — `low | medium | high` (GPT-5 Responses API verbosity override, 미설정 시 모델/프리셋 기본값)
- `--strict-config` — config.toml에 이 버전이 모르는 필드가 있으면 에러. **harness가 오타·구버전 키를 조기에 잡는 데 유용하다.**

권장 호출 형태(candidate):

```bash
codex exec \
  -m gpt-5.6-terra \
  -c model_reasoning_effort=high \
  --strict-config \
  -s workspace-write \
  -a on-request \
  "..."
```

---

## 3. 정규화 검증 — DESIGN.md §6.1 LOW/MEDIUM/HIGH/MAX 매핑

DESIGN.md §6.1은 다음을 요구한다.

> `effortLevel`은 provider 공통 정책 표현이다. Adapter는 이를 provider가 지원하는 실제 reasoning/effort 옵션으로 변환한다. (…) 지원하지 않는 조합은 Adapter가 조용히 변경하지 않고 routing validation error로 반환해야 한다.

### 3.1 4단계 → 실제 값 매핑

| `effortLevel` | Claude Code | Codex (GPT-5.6) | 상태 |
|---|---|---|---|
| `LOW` | `low` | `low` | 1:1 대응 |
| `MEDIUM` | `medium` | `medium` (기본값) | 1:1 대응 |
| `HIGH` | `high` (기본값) | `high` | 1:1 대응 |
| `MAX` | `max` | `max` | 1:1 대응 |
| — | **`xhigh` 표현 불가** | **`xhigh` 표현 불가** | **gap** |
| — | (`ultracode` — effort 아님) | (`ultra` — effort 아님) | 매핑 대상 아님 |
| — | 대응값 없음 | `none` 표현 불가 | gap (영향 작음) |
| — | 대응값 없음 | `minimal` (GPT-5.6 미지원) | 매핑 불필요 |

### 3.2 존재하지 않는 조합 (→ routing validation error 대상)

| # | 조합 | provider 실제 동작 | 심각도 |
|---|---|---|---|
| **G1** | `xhigh`를 4단계로 표현 불가 | 정책상 표현 자체가 안 됨. `HIGH`로 낮추면 권장 설정을 못 쓰고, `MAX`로 올리면 overthinking·비용 증가 | **높음** — Claude Code 문서는 `xhigh`를 "Deeper reasoning at higher token spend"로, Codex는 `xhigh`를 model-dependent 권장 단계로 둔다. coding/agentic이 dev-crew의 주 워크로드다 |
| **G2** | Claude Haiku 4.5 + 임의의 `effortLevel` | Haiku 4.5는 effort 미지원 모델. 문서 표에 없음 | **높음** — CHEAP tier를 Haiku로 잡으면 tier 전체가 effort 정책 밖에 놓임 |
| **G3** | Opus 4.6 / Sonnet 4.6 + `xhigh` | Claude Code가 `high`로 **조용히 하향** ("falls back to the highest supported level at or below") | 중간 — G1을 해소해 `XHIGH`를 도입할 경우 즉시 문제가 됨 |
| **G4** | `MAX` + settings 파일 영속화 | `effortLevel` 설정 키는 `low`/`medium`/`high`/`xhigh`만 받는다. `max`는 `CLAUDE_CODE_EFFORT_LEVEL` 환경변수 또는 세션별 `--effort`로만 가능 | **높음** — harness가 settings 파일로 MAX를 걸면 무시된다. 반드시 `--effort` 또는 env로 주입해야 한다 |
| **G5** | 비대화형(`-p`) + `/effort` | 세션 한정으로만 적용되고 default로 저장되지 않으며, model-default hold 중에는 `Not applied`를 보고한다. 문서 권고: "pass `--effort` at launch instead" | 중간 — harness는 전부 비대화형이므로 **`/effort` 경로를 아예 쓰면 안 된다** |
| **G6** | organization effort limit 초과 | 상한값으로 clamp. json/stream-json 출력·background agent에서는 **경고 없이** | 중간 — 요청 effort와 실제 effort가 조용히 갈라진다 |
| **G7** | Codex `none` | 4단계에 대응값 없음 | 낮음 — `LOW`로 흡수 가능. 다만 "추론 없음"과 "낮은 추론"은 다르다 |
| **G8** | Fable 5 + thinking off | Fable 5는 thinking을 끌 수 없다(`MAX_THINKING_TOKENS=0` 무효) | 낮음 — 현재 정책에 thinking off가 없으므로 영향 없음 |
| **G9** | `effortLevel`을 `ultracode`/`ultra`로 매핑 | 둘 다 model effort가 아니라 **harness 오케스트레이션 설정**이다 | 중간 — `effortLevel`에 절대 넣지 말 것. 넣으려면 별도 필드가 필요 |
| **G10** | Codex `--reasoning-effort` 플래그 사용 | 존재하지 않음. Adapter가 이 플래그를 생성하면 CLI 파싱 에러 | 낮음(하지만 구현 시 즉시 터짐) |

### 3.3 "조용한 하향 금지" 요구와의 충돌

DESIGN.md는 미지원 조합을 provider가 조용히 바꾸는 것을 금지하지만, **Claude Code는 기본 동작이 조용한 하향이다** (G3, G6). Codex도 모델별 지원 여부에 따라 값이 달리 처리될 수 있다.

따라서 Adapter는 provider 응답에 의존할 수 없고, **호출 전에 자체 (model, effortLevel) 지원 매트릭스로 검증**해야 한다. 최소한 다음이 필요하다.

1. Adapter가 소유하는 정적 지원 매트릭스 (모델 ID × effort 값)
2. 매트릭스에 없는 조합은 CLI를 호출하기 전에 routing validation error로 반환
3. Claude Code 버전 preflight (Opus 5는 v2.1.219+, Sonnet 5는 v2.1.197+ 등)
4. Codex는 `--strict-config`로 미지의 config 키를 조기 검출
5. `reasoningLevel`(실제 적용값) 기록은 요청값 echo가 아니라 별도 확인 경로 필요 — organization effort limit clamp는 관측되지 않음

### 3.4 권고 (티켓 #12 결정 입력)

**`effortLevel` enum을 5단계로 확장할 것을 권고한다.**

```
LOW | MEDIUM | HIGH | XHIGH | MAX
```

근거:

- 양 provider 모두 실제로 5단계를 갖는다. 4단계는 provider 공통분모가 아니라 **정보 손실**이다.
- `xhigh`는 Claude Code에서 Opus 4.7의 기본값이고, Codex에서는 model-dependent 상위 단계다. coding/agentic 워크로드에서 실제로 가장 자주 쓰이는 상위 단계다.
- 5단계로 하면 `LOW..MAX` 전 구간이 양 provider에 1:1로 대응해, 남는 gap이 G2(Haiku)와 G7(`none`)뿐으로 줄어든다.

`ultracode`(Claude Code)와 `ultra`(Codex)는 **model effort가 아니라 오케스트레이션 모드**이므로 `effortLevel`에 넣지 말고, 필요하면 `orchestrationMode` 같은 별도 필드로 분리한다. 흥미롭게도 양 provider가 동일한 개념(모델 스스로 멀티 에이전트/워크플로 오케스트레이션)을 각각 갖고 있어, dev-crew harness 자체 오케스트레이션과 **중복·충돌 가능성**이 있다 — 별도 조사 대상으로 남긴다.

---

## 4. tier → model ID 매핑 candidate (결정은 #12)

> 아래는 **후보안**이며 확정이 아니다. 근거만 정리한다.

### 4.1 Claude Code tier

| tier | candidate model ID | 근거 | 대안 |
|---|---|---|---|
| `CHEAP` | `claude-sonnet-5` @ `low` | Sonnet 5는 $2/$10로 legacy Sonnet 4.6($3/$15)보다 싸고 effort를 지원한다. Haiku($1/$5)보다 비싸지만 **effort 정책을 tier 전체에 균일 적용**할 수 있다 | `claude-haiku-4-5` — 최저가지만 **effort 미지원(G2)**. 쓰려면 CHEAP tier만 effort를 `N/A`로 두는 예외 규칙이 필요 |
| `DEFAULT` | `claude-sonnet-5` @ `high` | 문서상 "best combination of speed and intelligence", 1M context, 128k output, effort 전 구간 지원 | `claude-opus-5` @ `medium` |
| `HIGH_CAPABILITY` | `claude-opus-5` | "For complex agentic coding and enterprise work". $5/$25 | `claude-fable-5` — 최상위 capability이나 $10/$50(2배), ZDR 불가, usage credit 과금 가능. **escalation 최상단 전용**으로 두는 편이 안전 |

CHEAP와 DEFAULT가 같은 모델이고 effort만 다른 구성이 되는데, 이는 오히려 장점이다: **모델 전환 없이 effort만 조절해 escalation의 첫 단계를 만들 수 있고**, 비용 곡선이 연속적이다. 다만 DESIGN.md §7.6의 escalation이 "새 instance spawn"을 요구하므로 tier 정의가 (model, effort) 쌍이어야 함을 시사한다.

Fable 5를 escalation 최상단으로 둘 경우 추가 확인 필요:
- 조직의 ZDR 설정 (ZDR면 Fable 5 사용 불가)
- usage credit 과금 여부 — **비대화형 `-p` 모드에서는 동의 프롬프트 없이 과금**된다

### 4.2 Codex tier

| tier | candidate model ID | effort | 근거 |
|---|---|---|---|
| `CODEX_DEFAULT` | `gpt-5.6-terra` | `medium`~`high` | "Balanced GPT-5.6 model for everyday work, performance competitive with GPT-5.5 at a lower cost". $2/$12. DESIGN.md §7.4는 Local Review에 CODEX_DEFAULT + MEDIUM |
| `CODEX_HIGH_REASONING` | `gpt-5.6-sol` | `high`~`xhigh` | "Flagship GPT-5.6 model with the strongest capability for complex coding, computer use, research, and cybersecurity". $5/$30. DESIGN.md §7.4는 Integration/high-risk Review에 HIGH |
| (미할당) | `gpt-5.6-luna` | `low`~`medium` | $0.2/$1.2. 현재 DESIGN.md에 Codex CHEAP tier가 없다. Reviewer role이 Codex 전용이라 필요성은 낮지만, 향후 대량 리뷰 fan-out 시 후보 |

`gpt-5.4` / `gpt-5.4-mini`는 **2026-08-31 은퇴**하므로 후보에서 제외한다.

### 4.3 매핑 후 남는 gap 요약

5단계 enum + 위 candidate를 채택하면:

- G1 해소 (XHIGH 도입)
- G2는 CHEAP를 Sonnet 5로 두면 해소, Haiku를 쓰면 잔존
- G3는 legacy 4.6 모델을 매핑에서 제외하면 잔존하지 않음
- G4, G5, G6, G9, G10은 **enum 확장으로 해소되지 않는 Adapter 구현 제약**이다. 티켓 #12가 아니라 Adapter 구현 티켓에서 다뤄야 한다

---

## 5. 미해결 / 후속 조사 후보

1. **실제 적용된 effort의 관측 방법** — organization effort limit clamp는 json 출력에서 보이지 않는다. `--output-format json`의 `modelUsage` 필드가 effort까지 담는지 미확인. `ModelRoutingEvent.reasoningLevel`의 신뢰도에 직결된다.
2. **Codex `plan_mode_reasoning_effort`** — 바이너리에 존재하나 공개 config reference에서 확인하지 못했다. ARCHITECT role 설계와 관련 가능성.
3. **`ultracode` / `ultra` 오케스트레이션 모드와 dev-crew harness의 충돌** — 양쪽 모두 모델이 스스로 워크플로/서브에이전트를 오케스트레이션한다. harness 오케스트레이션과 이중화되면 관측성과 비용 예측이 무너진다.
4. **subagent 모델 fallback의 무보고 동작** — Claude Code는 teammate 모델 fallback을 보고하지 않는다. harness가 지정한 모델로 실제 실행됐는지 확인할 경로가 필요하다.
5. **Codex config reference 문서의 lag** — `model_reasoning_effort` 허용값 목록이 실제 바이너리와 불일치한다. 버전 업 시 재검증 루틴이 필요하다.

---

## 출처 목록

Claude Code / Anthropic:
- <https://code.claude.com/docs/en/model-config> — model alias 표, provider별 alias 해석, effort 레벨 표, 설정 경로와 우선순위, organization effort limits, adaptive reasoning, `_SUPPORTED_CAPABILITIES`
- <https://platform.claude.com/docs/en/about-claude/models/overview> — model ID, 가격, context window, max output, legacy 모델 표
- <https://platform.claude.com/docs/en/build-with-claude/effort> (model-config에서 참조)

Codex / OpenAI:
- <https://learn.chatgpt.com/docs/config-file/config-reference> — `model_reasoning_effort`, `model_reasoning_summary`, `model_verbosity`, profile 파일 경로
- <https://learn.chatgpt.com/docs/config-file/config-basic> — config.toml 예시, CLI override, 우선순위
- <https://learn.chatgpt.com/docs/models> — Codex 모델 목록과 tier 설명, GPT-5.4 은퇴 일정
- <https://learn.chatgpt.com/docs/developer-commands?surface=cli> — CLI 플래그
- <https://developers.openai.com/api/docs/models/gpt-5.6-sol> / `gpt-5.6-terra` / `gpt-5.6-luna` — slug, reasoning.effort 지원값과 기본값, 가격

로컬 1차 검증:
- `codex-cli 0.145.0` (`/opt/homebrew/bin/codex`) — `codex --help`, `codex exec --help` 전체 옵션 목록, 바이너리 내장 serde enum 문자열 및 subagent 도구 설명문
