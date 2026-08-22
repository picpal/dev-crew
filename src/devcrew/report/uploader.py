"""리포트 업로드 — Workers KV (wrangler 로그인 세션 사용, S3 키 불필요).

R2 → KV 전환 (사용자 결정 2026-08-19: 무료 사용 전제, 카드 등록 불필요).
키 스킴은 R2 시절과 동일(`reports/{id}/index.html`)해 worker 서빙 경로가 유지된다.

REPORT_BASE_URL이 없으면 업로드하지 않고 None을 반환한다 (링크 없이 텍스트만
전달하는 안전 폴백 — Slack 흐름을 막지 않는다).
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

# wrangler.toml(KV 바인딩)이 있는 worker 디렉토리 — account/binding 컨텍스트 제공
WORKER_DIR = Path(__file__).resolve().parents[3] / "worker"


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
        r = subprocess.run(
            ["npx", "wrangler", "kv", "key", "put", key, "--path", tmp,
             "--binding", "REPORTS", "--remote"],
            cwd=WORKER_DIR, capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise ReportUploadError(f"wrangler kv put 실패: {r.stderr.strip()[:300]}")
    finally:
        os.unlink(tmp)
    return f"{base}/tasks/{task_id}"
