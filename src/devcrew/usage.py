"""컨텍스트 점유 계산과 표기 — 창이 차오르는 걸 사용자가 미리 보게 한다."""
from __future__ import annotations

from .schema import Usage

CONTEXT_BADGE_AT = 0.40      # 이 비율부터 답변 맨 위에 점유율을 표기한다


def context_used(u: Usage) -> int:
    """이 turn이 실제로 점유한 컨텍스트 창 크기 추정.

    resume된 세션은 대화 전체가 매 turn의 입력이 된다 — 캐시된 prefix는
    cache_read/cache_creation(Claude) 또는 cached_input(Codex)로 분리 보고되므로
    창 점유량은 그 합계 + 신규 입력 + 출력이다. 누적 과금 토큰과 다른 값이다.
    """
    if u is None:
        return 0
    return ((u.input_tokens or 0) + (u.output_tokens or 0)
            + (u.cache_read_input_tokens or 0) + (u.cache_creation_input_tokens or 0)
            + (u.cached_input_tokens or 0))


def context_pct(used: int, window: int) -> int:
    if not window or used <= 0:
        return 0
    return min(100, round(used / window * 100))


def context_badge(used: int, window: int, *, at: float = CONTEXT_BADGE_AT,
                  pct: float | None = None) -> str:
    """`[context usage : 41%]` — 임계 미만이면 빈 문자열.

    compact/clear 시점을 사람이 판단하려면 남은 여유가 보여야 한다. 항상 붙이면
    잡음이라 창이 실제로 차오를 때부터만 표기한다. `pct`가 주어지면(어댑터 실측)
    그 값을 쓰고, 없을 때만 used/window 추정으로 계산한다."""
    p = round(pct) if pct is not None else context_pct(used, window)
    p = max(0, min(100, int(p)))
    return f"[context usage : {p}%]" if p >= round(at * 100) else ""


async def measure(adapter, session_id: str) -> dict | None:
    """어댑터가 실제 창 점유를 알면 그 값을 돌려준다 (CLI `/context`와 같은 데이터).

    토큰 합산 추정은 캐시 회계·시스템 프롬프트·툴 정의를 정확히 반영하지 못한다 —
    SDK가 아는 값이 있으면 그걸 쓰고, 없을 때만(세션이 이 프로세스 밖 등) 추정한다."""
    fn = getattr(adapter, "context_usage", None)
    if fn is None:
        return None
    try:
        return await fn(session_id)
    except Exception:
        return None
