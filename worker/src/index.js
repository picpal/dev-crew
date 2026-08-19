// GET /tasks/{taskId} → KV reports/{taskId}/index.html (§15.4, KV 전환 2026-08-19)
// POST /answer → 리포트 폼 선택 답변을 Slack 스레드에 게시 (brain 인터뷰 회신 경로)
// 접근 제어는 Cloudflare Access가 Worker 앞단에서 담당 (#5)
const CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; form-action 'self';";
const MARKER = "\ud83d\udce9 \uc120\ud0dd \ub2f5\ubcc0:"; // 📩 선택 답변:

const page = (title, body) => new Response(
  `<!doctype html><html lang="ko"><head><meta charset="utf-8"><title>${title}</title>
<style>body{font-family:sans-serif;max-width:600px;margin:4rem auto;color:#1a1a1a}</style>
</head><body><h2>${title}</h2>${body}</body></html>`,
  { headers: { "content-type": "text/html; charset=utf-8", "content-security-policy": CSP } });

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (request.method === "POST" && url.pathname === "/answer") {
      const form = await request.formData();
      const channel = form.get("channel") || "";
      const thread = form.get("thread_ts") || "";
      if (!/^[A-Z0-9]+$/.test(channel) || !/^\d+\.\d+$/.test(thread))
        return page("잘못된 요청", "<p>채널/스레드 정보가 없습니다.</p>");
      const picked = form.getAll("choice").filter(Boolean);
      const extra = (form.get("extra") || "").trim().slice(0, 2000);
      let text = picked.join(", ");
      if (extra) text += (text ? " / " : "") + extra;
      if (!text) return page("선택 없음", "<p>답변을 선택하거나 입력한 뒤 전달하세요.</p>");
      const res = await fetch("https://slack.com/api/chat.postMessage", {
        method: "POST",
        headers: { authorization: `Bearer ${env.SLACK_BOT_TOKEN}`,
                   "content-type": "application/json; charset=utf-8" },
        body: JSON.stringify({ channel, thread_ts: thread, text: `${MARKER} ${text}` }),
      });
      const out = await res.json();
      if (!out.ok) return page("전달 실패", `<p>Slack 오류: ${out.error}</p>`);
      return page("전달 완료 ✅", "<p>선택한 답변이 Slack 스레드에 게시됐습니다. 이 탭은 닫아도 됩니다.</p>");
    }

    const m = url.pathname.match(/^\/tasks\/([\w-]+)$/);
    if (!m) return new Response("not found", { status: 404 });
    const html = await env.REPORTS.get(`reports/${m[1]}/index.html`, { type: "stream" });
    if (!html) return new Response(`no report for ${m[1]}`, { status: 404 });
    return new Response(html, {
      headers: { "content-type": "text/html; charset=utf-8",
                 "content-security-policy": CSP, "cache-control": "no-cache" },
    });
  },
};
