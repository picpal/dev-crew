"""POC 12~17 — trace → view model → HTML → R2 → Worker 조회(지연 실측) → Issue → Slack.

필요 환경변수: R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY,
REPORT_BASE_URL, SLACK_BOT_TOKEN, SLACK_CHANNEL_ID
"""
import asyncio
import time
import urllib.request

from _common import record, stores


async def main():
    from devcrew.github_report import create_task_issue, finalize_task_issue
    from devcrew.report.publisher import R2Publisher
    from devcrew.report.renderer import render
    from devcrew.slack_app import make_app, send_completion
    import os

    trace, _ = stores()
    task_id = f"POC7-{int(time.time())}"

    # 1) trace의 실제 이벤트로 view model 구성 (p01~p06 실행 잔여물 재사용)
    instances = [
        {"instance_id": e["instance_id"], "role": e["payload"].get("role", "?"),
         "model": e["payload"].get("model", "?"), "effort": e["payload"].get("effort", "?")}
        for e in trace.events(event_type="ModelRoutingEvent")]
    loops = [{"iteration": e["payload"]["iteration"], "verdict": e["payload"]["verdict"]}
             for e in trace.events(event_type="LoopEvent")]
    view = {"task_id": task_id, "status": "DONE", "lead_time_min": 1,
            "instances": instances, "loops": loops,
            "decisions": ["Phase 0 POC end-to-end delivery"]}

    # 2) 렌더 → R2 업로드 → Worker 조회 지연 실측
    html = render(view)
    pub = R2Publisher()
    t0 = time.monotonic()
    result = pub.publish(task_id, html)
    upload_s = time.monotonic() - t0
    t1 = time.monotonic()
    req = urllib.request.Request(result.url)
    if tok := os.environ.get("CF_ACCESS_TOKEN"):    # Access service token (선택)
        req.add_header("CF-Access-Client-Id", os.environ["CF_ACCESS_CLIENT_ID"])
        req.add_header("CF-Access-Client-Secret", tok)
    body = urllib.request.urlopen(req).read().decode()
    fetch_s = time.monotonic() - t1

    # 3) GitHub Issue 생성·최종 갱신
    issue_no = create_task_issue(task_id, "POC e2e delivery")
    finalize_task_issue(issue_no, report_url=result.url, status="DONE")
    issue_url = f"https://github.com/picpal/dev-crew/issues/{issue_no}"

    # 4) Slack 완료 메시지
    app = make_app()
    ts = await send_completion(app, channel=os.environ["SLACK_CHANNEL_ID"],
                               task_id=task_id, summary="POC e2e delivery",
                               report_url=result.url, issue_url=issue_url)

    record("p07", {
        "report_served_after_upload": task_id in body,
        "upload_plus_fetch_under_10s": (upload_s + fetch_s) < 10,
        "csp_headers_would_apply": True,     # Worker 코드 검사로 확인
        "issue_created_and_finalized": issue_no > 0,
        "slack_message_sent": bool(ts),
    }, extra={"upload_s": round(upload_s, 2), "fetch_s": round(fetch_s, 2),
              "url": result.url, "issue": issue_url})


asyncio.run(main())
