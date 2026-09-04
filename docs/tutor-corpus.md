# 학습 코퍼스 — 코드가 아닌 자료로 `@tutor` 돌리기

`@tutor`는 repo 접두를 강제한다. UX 제약이 아니라 **품질 관문 셋이 전부 파일 경로에
매여 있기** 때문이다.

| 관문 | 코드 | repo가 없으면 |
|---|---|---|
| 인용 대조 (LLM 없음, 결정적) | `quiz._check_evidence` — 하네스가 파일을 열어 그 줄 범위에 그 문장이 있는지 본다 | 사라진다 |
| 교차 검증 | `tutor._verify` — `TUTOR_VERIFIER`가 **repo를 직접 읽어** 재판정한다 | 근거 없는 LLM 의견만 남는다 |
| 오답 노트 | `quiz.note_id(user, repo)` — 취약 영역이 쌓이는 축 | 축 자체가 없다 |

그래서 "주제만 던지는 학습"을 하려면 이 셋을 대체할 grounding을 새로 설계해야 한다.
**대신 자료를 파일로 두면 셋이 그대로 산다** — 코드 변경 0줄. 그것이 학습 코퍼스다.

## 만드는 법

`workspace_roots`(`~/Desktop/workspace`) 바로 아래에 git repo를 만들면 디렉토리명으로
**자동 등록**된다(`repos.discover_repos`). 커밋은 없어도 된다 — `.git`의 존재만 본다.

```bash
mkdir -p ~/Desktop/workspace/study-<주제> && cd $_ && git init
# 자료를 넣는다
launchctl kickstart -k gui/$(id -u)/com.picpal.devcrew-slack-bridge   # registry는 기동 시 1회 로드
```

그다음 `@tutor study-<주제>:`.

repo 이름은 `[A-Za-z0-9][A-Za-z0-9._-]*` 여야 한다(`repos._AUTO_NAME_RE`). 한글·공백·`@`가
든 디렉토리는 Slack 접두로 지목할 수 없어 자동 등록에서 제외된다. 디렉토리와 파일 이름은
한글이어도 된다 — 제약은 **repo 이름**에만 걸린다.

## 자료 규약

### 1. UTF-8 텍스트만

`_check_evidence`는 `read_text(encoding="utf-8")`로 파일을 연다. 실패하면
`path-unreadable`이고, 근거가 하나라도 버려지면 **그 문항이 통째로 폐기된다**
(`verify_citations`: "하나만 진짜고 나머지가 장식이면 그 장식이 정답의 근거인 척할 수 있다").

PDF·이미지·docx·epub을 그대로 넣으면 자료가 있는데 문항이 0개로 나온다. 텍스트로
변환해서 넣는다.

### 2. 요약하지 말고 원문을 남긴다

출제 모델은 `quote`에 **파일에 실제로 있는 문장**을 옮겨야 하고, 하네스가 그 줄 범위를
열어 대조한다(공백은 정규화하지만 문장 자체는 정규화하지 않는다). 자료가 내 말로 요약된
메모라면 문항의 근거도 그 요약에 갇힌다. 인용할 가치가 있는 문장은 원문으로 남긴다.

### 3. 디렉토리 하나 = 영역 하나

`area`는 Slack 헤더에 찍히고 리포트의 영역별 카드가 되며, `tutor.select_ten`이 영역
다양성으로 회차를 흩뿌린다. role 프롬프트는 한 회차의 영역을 **5개 이하**로 유지하라고
지시한다. 자료를 주제 디렉토리로 나눠 두면 그 경계가 그대로 영역이 된다.

### 4. 한 repo = 한 주제 묶음

회차의 범위는 repo 단위다 — 접두 뒤의 나머지 텍스트는 지금 버려진다
(`slack_tutor.on_mention`의 `repo_name, _rest = split_repo_prefix(...)`). 코퍼스 하나에
주제 열 개를 쌓으면 회차마다 무엇이 나올지 고를 수 없고, 오답 노트도 한 덩어리로 섞인다
(`note_id`는 repo 단위다).

주제가 늘면 repo를 쪼갠다. 자동 등록이라 비용은 디렉토리 하나뿐이고, `study-` 접두로
코드 repo와 목록에서 구분된다.

### 5. 문항이 적게 나오는 것은 정상이다

"근거를 찾지 못해 N문항만 출제했습니다"는 관문이 일한 결과다. 자료가 얇거나, 서술이
모호해 오답 보기를 명백히 틀리게 만들 수 없을 때 나온다. 숫자를 채우려 지어내지 않는
것이 이 파이프라인의 설계다.

## 안전

`TUTOR`·`TUTOR_VERIFIER`·`TUTOR_TA`·`TUTOR_CODE`는 `enforcement.ROLE_POLICY`에서
**읽기 도구만** 갖는다. 코퍼스 repo에 쓰지 않는다. 그리고 tutor role의 cwd는 worktree가
아니라 **실제 repo**다(불변조건 3) — 코퍼스에 개인 정보를 넣으면 그것이 모델에 그대로
읽힌다는 뜻이다. 넣기 전에 그 전제를 확인한다.
