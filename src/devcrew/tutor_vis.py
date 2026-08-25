"""TUTOR_VIS — 리포트에 넣을 다이어그램 하나를 `vision` 스킬로 그린다.

**작업 공간이 이 모듈의 핵심이다.** 그리기는 파일을 만들어야 해서 쓰기·Bash가 필요한데,
다른 tutor role은 cwd가 **사용자의 실제 repo**다(worktree가 아니다). 거기에 쓰기를 주면
학습 도구가 사용자 저장소를 더럽힌다. 그래서 이 role만 따로 두고, 매번 임시 디렉토리를
만들어 cwd로 준 뒤 끝나면 지운다 — 권한이 repo에 닿을 수 없다.

**모델이 낸 SVG는 비신뢰 입력이다.** 리포트의 CSP는 backstop이지 1차 방어가 아니므로
(worker/src/index.js), 스크립트·이벤트 핸들러·외부 참조·foreignObject를 여기서 뗀다.
"""
from __future__ import annotations

import asyncio
import re
import shutil
import tempfile
from pathlib import Path

from .schema import Role

TURN_TIMEOUT = 420.0
MAX_SVG = 120_000          # 리포트 한 장에 들어갈 만한 상한

INTRO = ("아래 설명을 다이어그램 하나로 그려라. vision 스킬을 Skill 도구로 호출해서 "
         "그리고, 결과를 작업 디렉토리에 `diagram.html`로 저장해라. 스타일은 기본값으로 "
         "진행한다 — 브랜드 토큰을 묻지 마라(이 세션에는 답할 사람이 없다).\n\n{spec}\n")

_SVG_RE = re.compile(r"<svg\b[^>]*>.*?</svg>", re.S | re.I)
# 통째로 들어내는 요소들. `foreignObject`는 SVG 안에 임의 HTML을 심는 통로라 뗀다.
_DROP_EL_RE = re.compile(
    r"<\s*(script|foreignObject|iframe|object|embed|animate|set)\b.*?"
    r"<\s*/\s*\1\s*>|<\s*(script|foreignObject|iframe|object|embed)\b[^>]*/\s*>",
    re.S | re.I)
_ON_ATTR_RE = re.compile(r"\son[a-zA-Z]+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", re.I)
# href/src는 자기 문서 안(#id)과 data: 이미지만 남긴다 — 그 외는 외부 요청이거나
# `javascript:` 실행이다. CSP가 막더라도 남겨 둘 이유가 없다.
_URL_ATTR_RE = re.compile(
    r"\s(?:xlink:)?(?:href|src)\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", re.I)


def sanitize_svg(svg: str) -> str:
    """모델이 낸 SVG에서 실행 가능한 것과 외부를 부르는 것을 뗀다."""
    out = _DROP_EL_RE.sub("", svg)
    out = _ON_ATTR_RE.sub("", out)

    def _url(m: re.Match) -> str:
        v = m.group(1).strip("\"'").strip()
        return m.group(0) if v.startswith("#") or v.startswith("data:image/") else ""

    return _URL_ATTR_RE.sub(_url, out)



# ── 테마 맞추기 ─────────────────────────────────────────────────────────────
# 리포트는 테마 반응형이다(`prefers-color-scheme`). vision은 자기 기본 팔레트(밝은 종이)
# 로 그리므로 그대로 실으면 다크 모드에서 흰 덩어리가 앉는다. 그렇다고 vision의 **다크
# 템플릿**을 고르면 이번엔 라이트가 어긋난다 — 한쪽을 고르는 문제가 아니다.
# 그래서 색을 우리 토큰으로 바꿔 그림이 페이지 테마를 그대로 따라가게 한다.
#
# 같은 hex라도 **속성에 따라 뜻이 다르다**: `fill="#2d3142"`는 글자, `stroke`는 테두리다.
# 테두리까지 `--ink-1`로 보내면 다크에서 순백 테두리가 되어 그림이 소리친다.
# 폴백(`var(--x, #원래색)`)을 남겨 토큰이 없는 곳에서도 원래대로 그려진다.
_PALETTE: dict[tuple[str, str], str] = {
    ("fill", "#ffffff"): "--surface",        # 상자 채움
    ("fill", "#f5f5f5"): "--surface-2",      # 종이 + 화살표 라벨 마스크 (배경과 같아야 한다)
    ("fill", "#2d3142"): "--ink-1",          # 글자
    ("fill", "#4f5d75"): "--ink-2",          # 보조 글자
    ("fill", "#0f766e"): "--good",           # 강조 (정상 경로)
    ("stroke", "#ffffff"): "--surface",
    ("stroke", "#f5f5f5"): "--surface-2",
    # 테두리·화살표는 **내용**이지 미세 구분선이 아니다. `--line-strong`으로 보내면
    # 다크에서 `#383835`가 되어 화살표와 점선 테두리가 배경에 묻힌다(실측 2026-08-25).
    # 원본의 두 단 위계(진한 테두리 / 흐린 화살표)를 잉크 두 단으로 옮긴다.
    ("stroke", "#2d3142"): "--ink-2",        # 상자 테두리
    ("stroke", "#4f5d75"): "--ink-3",        # 화살표·점선
    ("stroke", "#0f766e"): "--good",
}
_ACCENT_TINT = "rgba(15,118,110,0.08)"
_ACCENT_TINT_TOKEN = "color-mix(in srgb, var(--good, #0f766e) 8%, transparent)"
_COLOR_ATTR_RE = re.compile(r'\b(fill|stroke)="([^"]+)"')


def theme_svg(svg: str) -> str:
    """vision 기본 팔레트 → 리포트 토큰. 모르는 색은 건드리지 않는다.

    뜻을 모르는 색을 옮기면 그림이 망가진다 — 매핑에 없는 값은 그대로 둔다.
    (그래서 TUTOR_VIS 프롬프트가 "기본 팔레트를 벗어나지 마라"고 못박는다.)
    """
    def _sub(m: re.Match) -> str:
        attr, val = m.group(1), m.group(2).strip()
        if val.replace(" ", "").lower() == _ACCENT_TINT:
            return f'{attr}="{_ACCENT_TINT_TOKEN}"'
        token = _PALETTE.get((attr, val.lower()))
        return f'{attr}="var({token}, {val})"' if token else m.group(0)

    return _COLOR_ATTR_RE.sub(_sub, svg or "")


def extract_svg(html: str) -> str | None:
    """vision이 낸 HTML에서 `<svg>` 한 덩어리만 떼어 낸다.

    페이지 전체를 쓰지 않는다 — 우리 리포트는 단일 파일이고, vision 페이지에는 Google
    Fonts 링크 같은 외부 참조가 붙는다(우리 CSP가 막는다). SVG는 `var(--)`에 기대지
    않아 그대로 들어내도 그려진다(2026-08-25 실측).
    """
    m = _SVG_RE.search(html or "")
    if not m:
        return None
    svg = theme_svg(sanitize_svg(m.group(0)))
    return svg if 0 < len(svg) <= MAX_SVG else None


async def draw(orch, cfg, *, exec_id: str, spec: str) -> str | None:
    """설명 → 살균된 `<svg>` 문자열. 못 그리면 None.

    **실패는 조용히 None이다.** 그림은 리포트의 곁다리라, 못 그렸다고 답변을 막으면
    안 된다 — 호출자는 그림 없이 그대로 낸다.
    """
    work = Path(tempfile.mkdtemp(prefix="devcrew-vis-"))
    inst = sid = None
    try:
        inst = await orch.spawn(Role.TUTOR_VIS, cfg.role_defaults[Role.TUTOR_VIS].tier,
                                execution_id=exec_id, node_id="diagram",
                                task_scope="리포트 다이어그램", worktree=str(work))

        async def _turn():
            nonlocal sid
            sid = await orch.start_worker(inst, INTRO.format(spec=spec))
            return await orch.adapters[inst.provider].send(
                sid, "이제 결과를 스키마대로 제출해라.")

        out = await asyncio.wait_for(_turn(), timeout=TURN_TIMEOUT)
        raw = out.structured if isinstance(out.structured, dict) else {}
        if raw.get("status") != "PASS":
            return None
        name = str(raw.get("file") or "diagram.html")
        # 모델이 낸 이름은 경로다 — `../`로 작업 공간 밖을 가리키면 읽지 않는다.
        try:
            target = (work / name).resolve()
            target.relative_to(work.resolve())
        except (ValueError, OSError):
            return None
        if not target.is_file():
            return None
        return extract_svg(target.read_text(errors="replace"))
    except Exception:
        return None
    finally:
        if inst is not None:
            if sid is not None:
                try:
                    await orch.adapters[inst.provider].archive(sid)
                except Exception:
                    pass
            try:
                orch.registry.finish(inst.instance_id)
            except Exception:
                pass
        shutil.rmtree(work, ignore_errors=True)
