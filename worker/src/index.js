// GET /tasks/{taskId} → KV reports/{taskId}/index.html (§15.4, KV 전환 2026-08-19)
// 접근 제어는 Cloudflare Access가 Worker 앞단에서 담당 (#5)
const CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:;";

export default {
  async fetch(request, env) {
    const m = new URL(request.url).pathname.match(/^\/tasks\/([\w-]+)$/);
    if (!m) return new Response("not found", { status: 404 });
    const html = await env.REPORTS.get(`reports/${m[1]}/index.html`, { type: "stream" });
    if (!html) return new Response(`no report for ${m[1]}`, { status: 404 });
    return new Response(html, {
      headers: {
        "content-type": "text/html; charset=utf-8",
        "content-security-policy": CSP,
        "cache-control": "no-cache",
      },
    });
  },
};
