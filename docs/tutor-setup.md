# @tutor 실행 준비

`@tutor`는 `@brain`·`@crew`와 **별도 Slack 앱**이다. 인터뷰 세션과 퀴즈 세션이 한 스레드
공간을 쓰면 명령이 충돌하고, 봇 정체성도 섞인다.

## 1. Slack 앱 생성 (사용자 작업)

워크스페이스 권한이 필요해 하네스가 대신 할 수 없다.

1. api.slack.com/apps → **Create New App** → From scratch, 이름은 `tutor`
2. **Socket Mode** 켜기 → App-Level Token 생성 (scope: `connections:write`) → `xapp-…`
3. **OAuth & Permissions** → Bot Token Scopes:
   `app_mentions:read`, `chat:write`, `reactions:write`, `channels:history`, `groups:history`
4. **Event Subscriptions** → Subscribe to bot events: `app_mention`
5. **Interactivity & Shortcuts** 켜기 (버튼 응답을 받는다)
6. 워크스페이스에 설치 → Bot User OAuth Token `xoxb-…`
7. 사용할 채널에 `/invite @tutor`

## 2. 환경 변수

```bash
export TUTOR_BOT_TOKEN=xoxb-...
export TUTOR_APP_TOKEN=xapp-...
```

두 값이 **모두** 있어야 앱이 뜬다 (`slack_engine`이 게이팅한다). 없으면 조용히 건너뛰므로
brain·crew만 쓰던 환경은 그대로 돈다. 기동 로그에 `devcrew: @tutor 학습 앱 활성화`가 찍힌다.

## 3. 사용

```
@tutor message-gate:
```

`config/repos.yaml`에 등록된 repo 이름을 접두로 준다. `workspace_roots` 바로 아래의 git repo는
디렉토리명으로 **자동 등록**되므로 대개 따로 적을 것이 없다 — 다만 목록은 엔진 기동 시 한 번
읽으므로, workspace에 repo를 새로 만들었다면 엔진을 재시작해야 보인다.

출제는 그 repo를 읽는 agentic turn이라 수십 초가 걸린다. 문항은 버튼으로만 답한다 —
자유 답글은 받지 않는다.

- 진행 중에는 정답을 공개하지 않는다. 채점은 마지막에 한 번에 한다.
- 미완 회차는 24시간 안에는 같은 스레드에서 이어 풀 수 있다 (프로세스를 재시작해도).
- 오답은 사용자·repo 단위로 누적되고, 다음 회차에서 같은 지점을 맞히면 지워진다.

## 4. 문항이 적게 나올 때

"근거를 찾지 못해 N문항만 출제했습니다"는 정상 동작이다. 인용 대조나 교차 검증을 통과하지
못한 문항을 숫자를 맞추려고 지어내지 않는다. 문서가 얇은 repo일수록 자주 보게 된다 —
`CONTEXT.md`나 ADR이 있으면 문항 품질이 크게 올라간다.
