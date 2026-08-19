"""리포트 업로드 경로 선택 — R2 S3 키가 있으면 boto3, 없으면 wrangler CLI 인증 폴백.

REPORT_BASE_URL이 없으면 업로드하지 않고 None을 반환한다 (링크 없이 텍스트만
전달하는 안전 폴백 — Slack 흐름을 막지 않는다).
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

BUCKET = "dev-crew-reports"
# wrangler.toml이 있는 worker 디렉토리 — account 컨텍스트 제공
WORKER_DIR = Path(__file__).resolve().parents[3].parent / "phase0-poc" / "worker"


class ReportUploadError(Exception):
    pass


def publish_report(task_id: str, html: str) -> str | None:
    """HTML 업로드 → 공개 URL. REPORT_BASE_URL 미설정이면 None (업로드 생략)."""
    base = os.environ.get("REPORT_BASE_URL", "").rstrip("/")
    if not base:
        return None
    if os.environ.get("R2_ACCOUNT_ID") and os.environ.get("R2_ACCESS_KEY_ID"):
        from .publisher import R2Publisher
        return R2Publisher(bucket=BUCKET, public_base=base).publish(task_id, html).url
    return _wrangler_put(task_id, html, base)


def _wrangler_put(task_id: str, html: str, base: str) -> str:
    """S3 키 없이 wrangler 로그인 세션으로 업로드 (npx wrangler r2 object put)."""
    key = f"{BUCKET}/reports/{task_id}/index.html"
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False) as f:
        f.write(html)
        tmp = f.name
    try:
        r = subprocess.run(
            ["npx", "wrangler", "r2", "object", "put", key, "--file", tmp,
             "--content-type", "text/html; charset=utf-8", "--remote"],
            cwd=WORKER_DIR if WORKER_DIR.exists() else None,
            capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise ReportUploadError(f"wrangler put 실패: {r.stderr.strip()[:300]}")
    finally:
        os.unlink(tmp)
    return f"{base}/tasks/{task_id}"
