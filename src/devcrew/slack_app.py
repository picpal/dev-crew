"""Slack 전달 (§15.1, #8) — POC: 완료 메시지 발신 + Socket Mode 수신 확인."""
from __future__ import annotations

import os

from slack_bolt.async_app import AsyncApp


def make_app() -> AsyncApp:
    return AsyncApp(token=os.environ["SLACK_BOT_TOKEN"])


async def send_completion(app: AsyncApp, *, channel: str, task_id: str,
                          summary: str, report_url: str, issue_url: str) -> str:
    text = (f"✅ {task_id} 완료\n\nSummary: {summary}\n"
            f"Detailed Report: {report_url}\nGitHub Issue: {issue_url}")
    resp = await app.client.chat_postMessage(channel=channel, text=text)
    return resp["ts"]
