"""리포트 업로드 — Workers KV (wrangler 로그인 세션 사용, S3 키 불필요).

R2 → KV 전환 (사용자 결정 2026-08-19: 무료 사용 전제, 카드 등록 불필요).
키 스킴은 R2 시절과 동일(`reports/{id}/index.html`)해 worker 서빙 경로가 유지된다.

REPORT_BASE_URL이 없으면 업로드하지 않고 None을 반환한다 (링크 없이 텍스트만
전달하는 안전 폴백 — Slack 흐름을 막지 않는다).
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

# wrangler.toml(KV 바인딩)이 있는 worker 디렉토리 — account/binding 컨텍스트 제공
WORKER_DIR = Path(__file__).resolve().parents[3] / "worker"


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(text: str) -> str:
    """wrangler는 색 코드를 stderr에 섞어 보낸다. 그대로 trace에 남기면 사유가
    이스케이프 문자로 뒤덮여 읽히지 않는다 (실측: 300자 중 절반이 색 코드였다)."""
    return _ANSI_RE.sub("", text or "")


class ReportUploadError(Exception):
    pass


def publish_report(task_id: str, html: str) -> str | None:
    """HTML 업로드 → 공개 URL. REPORT_BASE_URL 미설정이면 None (업로드 생략)."""
    base = os.environ.get("REPORT_BASE_URL", "").rstrip("/")
    if not base:
        return None
    key = f"reports/{task_id}/index.html"
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False) as f:
        f.write(html)
        tmp = f.name
    try:
        _put(key, tmp)
    finally:
        os.unlink(tmp)
    return f"{base}/tasks/{task_id}"


# wrangler는 만료된 OAuth 토큰을 **요청이 한 번 실패한 뒤에** 갱신한다. 실측
# (2026-09-04 18:48:32): 토큰 만료 2초 뒤의 업로드가 `Authentication error [10000]`
# 으로 죽었고, 같은 명령을 다시 돌리자 성공했다. 한 번의 실패로 리포트를 포기하면
# 토큰이 만료될 때마다 링크를 잃는다 — 갱신 뒤 재시도가 정확한 처방이다.
UPLOAD_ATTEMPTS = 3
UPLOAD_BACKOFF = 3.0
# 60초는 실제로 모자랐다 (2026-09-04 18:0x, `npx` 콜드 스타트로 추정). 관측이 상한을
# 넘겼으므로 올린다 — 짐작이 아니라 실측 한 건에 근거한다 (lessons C3).
UPLOAD_TIMEOUT = 120.0


def _put(key: str, path: str) -> None:
    """`wrangler kv key put` — 일시 실패는 재시도한다. 마지막 사유를 실어 올린다."""
    last = ""
    for attempt in range(UPLOAD_ATTEMPTS):
        if attempt:
            time.sleep(UPLOAD_BACKOFF)
        try:
            r = subprocess.run(
                ["npx", "wrangler", "kv", "key", "put", key, "--path", path,
                 "--binding", "REPORTS", "--remote"],
                cwd=WORKER_DIR, capture_output=True, text=True, timeout=UPLOAD_TIMEOUT)
        except subprocess.TimeoutExpired:
            last = f"{UPLOAD_TIMEOUT:.0f}초 안에 끝나지 않음"
            continue
        if r.returncode == 0:
            return
        last = strip_ansi(r.stderr.strip())[:300]
    raise ReportUploadError(f"wrangler kv put 실패 ({UPLOAD_ATTEMPTS}회 시도): {last}")
