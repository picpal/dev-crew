// GET /tasks/{taskId} → R2 reports/{taskId}/index.html (§15.4)
// 접근 제어는 Cloudflare Access가 Worker 앞단에서 담당 (#5)
const CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:;";

export default {
  async fetch(request, env) {
    const m = new URL(request.url).pathname.match(/^\/tasks\/([\w-]+)$/);
    if (!m) return new Response("not found", { status: 404 });
    const obj = await env.REPORTS.get(`reports/${m[1]}/index.html`);
    if (!obj) return new Response(`no report for ${m[1]}`, { status: 404 });
    return new Response(obj.body, {
      headers: {
        "content-type": "text/html; charset=utf-8",
        "content-security-policy": CSP,
        "cache-control": "no-cache",
      },
    });
  },
};
