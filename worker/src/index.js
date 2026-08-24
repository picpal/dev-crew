// GET /tasks/{taskId} → KV reports/{taskId}/index.html (§15.4, KV 전환 2026-08-19)
// POST /answer → 리포트 폼 선택 답변을 Slack 스레드에 게시 (brain 인터뷰 회신 경로)
// 접근 제어는 Cloudflare Access가 Worker 앞단에서 담당 (#5)
const CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; form-action 'self';";
// 코드 실행 리포트(`code-*`)만 인라인 스크립트를 허용한다. **'unsafe-inline'이 아니라
// 해시다** — 하네스가 넣은 그 스크립트 하나만 돌고, 이스케이프 버그로 끼어든 스크립트는
// 여전히 막힌다. 그래서 backstop은 다른 리포트뿐 아니라 이 페이지에서도 살아 있다.
// 해시 원본: `src/devcrew/report/code_report.py: JS_SRC`.
// 어긋나면 스크립트가 조용히 차단되므로
// `tests/test_code_report.py::test_worker_csp_matches_the_script_hash`가 대조한다.
const CODE_SCRIPT_HASH = "sha256-RcLeU86abWMUnIPPnRECpLxPzpalL4NNLwqlZc2ILZI=";
const CODE_CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
  + `form-action 'self'; script-src '${CODE_SCRIPT_HASH}';`;
const MARKER = "\ud83d\udce9 \uc120\ud0dd \ub2f5\ubcc0:"; // 📩 선택 답변:

const esc = (v) => String(v).replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

// Slack이 준 오류 코드를 화면에 그대로 옮기기 전에 다듬는다.
// - ok:false인데 error가 없거나 문자열이 아니면 `undefined`/`[object Object]`가
//   진짜 오류 코드인 척 표시된다 → 모른다고 말한다
// - 양방향 제어문자(U+202A~U+202E 등)는 escape를 통과하지만 화면에서 글자 순서를
//   뒤집어 오류 값을 왜곡한다 → 걷어낸다
// - 길이를 제한한다: 오류 전문이 길면 복구 안내가 화면 밖으로 밀린다
const errCode = (e) => {
  if (typeof e !== "string" || !e.trim()) return "";
  const clean = e.replace(/[\u0000-\u001F\u007F\u200B-\u200F\u202A-\u202E\u2066-\u2069]/g, "");
  return clean.length > 120 ? clean.slice(0, 120) + "…" : clean;
};

// 상태 아이콘. CSP가 외부 이미지를 막으므로 인라인 SVG로 그린다. 색만으로 말하지
// 않도록 모양(✓ / ✕ / !)이 상태마다 다르다 — 다크 모드·색각 이상에서도 구분된다.
const ICON = {
  ok: '<path d="m8 12.4 2.9 2.9 5.1-6"/>',
  error: '<path d="m9 9 6 6m0-6-6 6"/>',
  warn: '<path d="M12 7.4v5.1"/><circle cx="12" cy="16.3" r=".95" fill="currentColor" stroke="none"/>',
};

// 팔레트는 리포트(src/devcrew/report/quiz_report.py)와 같은 토큰을 쓰되 이 페이지에
// 필요한 만큼만 가져왔다. **body에 명시적 background가 반드시 있어야 한다** — 없으면
// 다크 모드 브라우저가 배경을 검게 칠하고 글자색만 남아 검은 글씨가 된다.
const CSS = `
:root{color-scheme:light dark;
  --plane:#f9f9f7;--surface:#fcfcfb;--line:rgba(11,11,11,.10);
  --ink-1:#0b0b0b;--ink-2:#52514e;--ink-3:#6e6c67;
  --good:#006300;--bad:#c22f2f;--warn:#8a5a00;
  --code:rgba(11,11,11,.055);--shadow:rgba(11,11,11,.05)}
@media (prefers-color-scheme:dark){:root{
  --plane:#0d0d0d;--surface:#1a1a19;--line:rgba(255,255,255,.10);
  --ink-1:#fff;--ink-2:#c3c2b7;--ink-3:#93918a;
  --good:#0ca30c;--bad:#e66767;--warn:#e0a33a;
  --code:rgba(255,255,255,.07);--shadow:rgba(0,0,0,.3)}}
*{box-sizing:border-box}
body{margin:0;background:var(--plane);color:var(--ink-1);line-height:1.65;
  font-family:system-ui,-apple-system,"Apple SD Gothic Neo","Segoe UI",sans-serif;
  -webkit-text-size-adjust:100%;
  min-height:100dvh;display:grid;align-content:safe center}
.wrap{max-width:30rem;margin:0 auto;padding:clamp(1.4rem,8vw,4rem) clamp(.9rem,5vw,1.5rem)}
.card{background:var(--surface);border:1px solid var(--line);border-radius:14px;
  box-shadow:0 1px 2px var(--shadow);padding:clamp(1.1rem,5vw,1.6rem)}
.icon{display:block;width:1.9rem;height:1.9rem;color:var(--ink-2)}
.is-ok .icon{color:var(--good)}
.is-error .icon{color:var(--bad)}
.is-warn .icon{color:var(--warn)}
h1{margin:.65rem 0 0;font-size:clamp(1.1rem,4.6vw,1.35rem);font-weight:650;
  line-height:1.42;letter-spacing:-.01em;word-break:keep-all;overflow-wrap:anywhere}
p{margin:.55rem 0 0;color:var(--ink-2);font-size:.93rem;
  word-break:keep-all;overflow-wrap:anywhere}
.diag{margin-top:.9rem;padding-top:.7rem;border-top:1px solid var(--line);
  color:var(--ink-3);font-size:.84rem}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.86em;
  background:var(--code);border-radius:4px;padding:.05em .3em;overflow-wrap:anywhere}
`;

// title은 항상 escape한다. body는 호출부가 직접 쓴 마크업 조각이고, 그 안에 섞이는
// 외부 값(Slack API의 out.error 등)은 호출부에서 esc()를 통과시킨다.
// CSP는 backstop이지 1차 방어가 아니다.
const page = (kind, title, body) => new Response(
  `<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>${esc(title)}</title>
<style>${CSS}</style></head>
<body class="is-${kind}"><main class="wrap"><div class="card">
<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
 stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/>${ICON[kind] || ICON.warn}</svg>
<h1>${esc(title)}</h1>${body}</div></main></body></html>`,
  { headers: { "content-type": "text/html; charset=utf-8", "content-security-policy": CSP } });

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (request.method === "POST" && url.pathname === "/answer") {
      const form = await request.formData();
      const channel = form.get("channel") || "";
      const thread = form.get("thread_ts") || "";
      if (!/^[A-Z0-9]+$/.test(channel) || !/^\d+\.\d+$/.test(thread))
        return page("warn", "잘못된 요청", "<p>채널/스레드 정보가 없습니다. 리포트 링크를 다시 열어 답변을 전달해 주세요.</p>");
      const picked = form.getAll("choice").filter(Boolean);
      const extra = (form.get("extra") || "").trim().slice(0, 2000);
      let text = picked.join(", ");
      if (extra) text += (text ? " / " : "") + extra;
      if (!text) return page("warn", "선택 없음", "<p>답변을 선택하거나 입력한 뒤 전달하세요. 리포트 탭으로 돌아가면 고른 내용은 그대로 있습니다.</p>");
      const res = await fetch("https://slack.com/api/chat.postMessage", {
        method: "POST",
        headers: { authorization: `Bearer ${env.SLACK_BOT_TOKEN}`,
                   "content-type": "application/json; charset=utf-8" },
        body: JSON.stringify({ channel, thread_ts: thread, text: `${MARKER} ${text}` }),
      });
      const out = await res.json();
      if (!out.ok) {
        // 복구 안내가 먼저다. 오류 코드는 진단용이라 뒤에 둔다 — 앞에 두면 긴
        // 오류 문자열이 "무엇을 해야 하는지"를 화면 밖으로 밀어낸다.
        const code = errCode(out.error);
        return page("error", "전달 실패",
          "<p>잠시 후 다시 시도하거나, Slack 스레드에 직접 답글을 남겨 주세요.</p>"
          + (code ? `<p class="diag">Slack 오류: <code>${esc(code)}</code></p>`
                  : "<p class=\"diag\">Slack이 오류 코드를 주지 않았습니다.</p>"));
      }
      return page("ok", "전달 완료", "<p>선택한 답변이 Slack 스레드에 게시됐습니다. 이 탭은 닫아도 됩니다.</p>");
    }

    const m = url.pathname.match(/^\/tasks\/([\w-]+)$/);
    if (!m) return new Response("not found", { status: 404 });
    const html = await env.REPORTS.get(`reports/${m[1]}/index.html`, { type: "stream" });
    if (!html) return new Response(`no report for ${m[1]}`, { status: 404 });
    return new Response(html, {
      headers: { "content-type": "text/html; charset=utf-8",
                 "content-security-policy": m[1].startsWith("code-") ? CODE_CSP : CSP,
                 "cache-control": "no-cache" },
    });
  },
};
