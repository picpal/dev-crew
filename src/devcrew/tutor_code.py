"""TUTOR_CODE — 코드 한 덩어리의 실행 흐름을 스텝으로 푼다 (#19 후속).

산출물은 좌우 분할 리포트다: 왼쪽에 코드, 오른쪽에 스텝별 이유와 변수 상태. 2초에
한 칸씩 자동으로 넘어간다.

**코드 원문은 모델에서 받지 않는다.** 모델은 `file`·`start_line`·`end_line`만 지목하고
하네스가 그 파일을 직접 읽는다. 이유는 두 가지다.

1. 지어낸 코드가 화면에 오르지 않는다 — 학습 도구에서 그건 최악의 실패다
   (인용 대조를 기계로 하는 것과 같은 이유).
2. 스키마에 큰 필드가 하나(`steps`)만 남는다. 둘이면 순서로는 하나밖에 못 지키고,
   앞엣것이 길어지면 뒤엣것이 통째로 삼켜진다 (lessons C15).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from .schema import Role
from .untrusted import NOTE, fence

# 세션 하나의 전체 예산. 추적은 호출 대상까지 읽어야 해서 단순 답변보다 길다.
TURN_TIMEOUT = 420.0
MAX_LINES = 200           # 왼쪽 창에 띄울 코드 줄 수 상한
MIN_STEPS = 2             # 이보다 적으면 애니메이션이 아니다
MAX_VALUE = 120           # 변수 값 한 칸의 표시 상한


class TraceUnavailable(Exception):
    """추적을 만들지 못했다. 사유는 메시지에 있고 호출자가 그대로 쓴다."""


@dataclass
class Line:
    number: int
    text: str


@dataclass
class Var:
    name: str
    value: str
    changed: bool


@dataclass
class Step:
    line: int
    reason: str
    vars: list[Var] = field(default_factory=list)


@dataclass
class Trace:
    title: str
    role_of_code: str
    path: str
    lines: list[Line]
    steps: list[Step]
    dropped: int = 0
    diagram: str | None = None      # 무엇을 그릴지 (산문). 그리기는 TUTOR_VIS가 한다


INTRO = ("아래 repo에서 학습자가 물은 코드의 **실행 흐름**을 추적한다. " + NOTE + "\n"
         "{focus}\n"
         "대상을 찾아 읽고, 그 코드가 부르는 것들까지 확인한 뒤 스키마대로 제출해라.\n")


def _focus_block(path: str | None, symbol: str | None, question: str) -> str:
    lines = []
    if path:
        lines.append(f"파일: {path}")
    if symbol:
        lines.append(f"대상: {symbol}")
    lines.append(f"학습자 질문: {question}")
    return fence("focus", "\n".join(lines))


def resolve_source(repo_path: str, rel: str, start: int, end: int) -> list[Line]:
    """지목된 범위를 파일에서 읽는다. repo 밖이면 거부한다.

    이 파일을 여는 것은 워커가 아니라 **하네스**라 `enforcement`의 경로 게이트가
    걸리지 않는다 — 여기서 막지 않으면 `../../etc/passwd`가 그대로 열린다.
    """
    root = Path(repo_path).resolve()
    try:
        target = (root / str(rel or "")).resolve()
        target.relative_to(root)
    except (ValueError, OSError) as e:
        raise TraceUnavailable(f"repo 밖 경로를 지목했습니다: {rel!r}") from e
    if not target.is_file():
        raise TraceUnavailable(f"파일을 찾지 못했습니다: {rel!r}")
    try:
        body = target.read_text(errors="replace").splitlines()
    except OSError as e:
        raise TraceUnavailable(f"파일을 읽지 못했습니다: {type(e).__name__}") from e
    lo = max(1, int(start or 1))
    hi = min(len(body), max(lo, int(end or lo)))
    if lo > len(body):
        raise TraceUnavailable(f"{rel}에 {lo}번째 줄이 없습니다")
    hi = min(hi, lo + MAX_LINES - 1)
    return [Line(number=n, text=body[n - 1]) for n in range(lo, hi + 1)]


def parse_steps(raw, shown: set[int]) -> tuple[list[Step], int]:
    """모델이 낸 steps → Step. **화면에 없는 줄을 가리키면 버린다.**

    강조할 줄이 왼쪽에 없으면 그 스텝은 아무것도 가리키지 못한다. 조용히 범위로
    당기면 엉뚱한 줄이 강조되므로 — 거짓을 보여주느니 빼고 뺐다고 적는다 (C12).
    """
    out: list[Step] = []
    dropped = 0
    for r in raw or []:
        if not isinstance(r, dict):
            dropped += 1
            continue
        ln, reason = r.get("line"), r.get("reason")
        if (not isinstance(ln, int) or isinstance(ln, bool) or ln not in shown
                or not isinstance(reason, str) or not reason.strip()):
            dropped += 1
            continue
        vs: list[Var] = []
        for v in r.get("vars") or []:
            if not isinstance(v, dict):
                continue
            name, val = v.get("name"), v.get("value")
            if not isinstance(name, str) or not name.strip():
                continue
            text = "" if val is None else str(val)
            vs.append(Var(name=name.strip(), value=text[:MAX_VALUE],
                          changed=bool(v.get("changed"))))
        out.append(Step(line=ln, reason=reason.strip(), vars=vs))
    return out, dropped


async def trace_code(orch, cfg, *, exec_id: str, repo_path: str,
                     focus_path: str | None, focus_symbol: str | None,
                     question: str) -> Trace:
    """실행 추적 하나. 1회용 세션이므로 끝나면 반드시 반납한다.

    상한은 `spawn` 이후 전 구간을 감싼다 — 실제 작업(repo 읽기)이 첫 turn에서
    일어나므로 두 번째 turn에만 걸면 매달리는 쪽이 무방비다 (lessons C14).
    """
    tier = cfg.role_defaults[Role.TUTOR_CODE].tier
    inst = await orch.spawn(Role.TUTOR_CODE, tier, execution_id=exec_id,
                            node_id="codetrace", task_scope="코드 실행 흐름 추적",
                            worktree=repo_path)
    sid: str | None = None

    async def _turn():
        nonlocal sid
        intro = INTRO.format(focus=_focus_block(focus_path, focus_symbol, question))
        sid = await orch.start_worker(inst, intro)
        return await orch.adapters[inst.provider].send(
            sid, "이제 실행 스텝을 스키마대로 제출해라.")

    try:
        try:
            out = await asyncio.wait_for(_turn(), timeout=TURN_TIMEOUT)
        except asyncio.TimeoutError as e:
            raise TraceUnavailable("TimeoutError") from e
        except TraceUnavailable:
            raise
        except Exception as e:
            raise TraceUnavailable(f"{type(e).__name__}: {e}") from e

        raw = out.structured if isinstance(out.structured, dict) else {}
        if raw.get("status") != "PASS":
            raise TraceUnavailable(f"추적 실패 (status={raw.get('status')!r})")
        lines = resolve_source(repo_path, raw.get("file"),
                               raw.get("start_line"), raw.get("end_line"))
        steps, dropped = parse_steps(raw.get("steps"), {ln.number for ln in lines})
        if len(steps) < MIN_STEPS:
            raise TraceUnavailable(
                f"화면 범위 안의 스텝이 {len(steps)}개뿐입니다 (버린 스텝 {dropped}개)")
        spec = raw.get("diagram")
        return Trace(title=str(raw.get("title") or "코드 실행 흐름"),
                     diagram=(spec.strip() if isinstance(spec, str) and spec.strip()
                              else None),
                     role_of_code=str(raw.get("role_of_code") or ""),
                     path=str(raw.get("file") or ""), lines=lines, steps=steps,
                     dropped=dropped)
    finally:
        # 프로세스(`archive`)와 registry 행(`finish`)을 둘 다 반납한다 — `finish`만
        # 하면 행은 닫히는데 워커는 계속 돈다 (2026-08-24).
        if sid is not None:
            try:
                await orch.adapters[inst.provider].archive(sid)
            except Exception:
                pass
        try:
            orch.registry.finish(inst.instance_id)
        except Exception:
            pass
