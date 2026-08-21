"""worker/src/index.js의 안내 페이지 — escape와 렌더 규약.

이 저장소에는 JS 테스트 러너가 없다. 그렇다고 검증을 안 하면, 외부 값(Slack API의
`out.error`)이 escape 없이 HTML에 박히던 경로가 조용히 되살아난다. `node`가 있으면
**실제로 렌더해서** 본다 — 소스를 grep하는 테스트는 배선을 증명하지 못한다(lessons C1).
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

WORKER = Path(__file__).resolve().parents[1] / "worker" / "src" / "index.js"

# page()/esc()/CSS만 떼어 Worker 런타임 없이 부른다. `new Response(...)`를 벗겨
# 문자열만 돌려받는다 — 우리가 보려는 것은 헤더가 아니라 마크업이다.
_HARNESS = r"""
import fs from "fs";
const src = fs.readFileSync(process.argv[2], "utf8");
const head = src.slice(0, src.indexOf("export default"))
  .replace("const page = (kind, title, body) => new Response(",
           "const page = (kind, title, body) => (")
  .replace(/,\n  \{ headers: \{ "content-type"[\s\S]*?\}\);/, ");");
const m = await import("data:text/javascript," +
  encodeURIComponent(head + "\nexport {page, esc, CSS};"));
const payload = '<img src=x onerror=alert(1)>';
console.log(JSON.stringify({
  html: m.page("error", `잘못된 요청 ${payload}`,
               `<p><code>${m.esc(payload)}</code></p>`),
  css: m.CSS,
  escaped: ["&", "<", ">", '"', "'"].map((c) => m.esc(c)),
}));
"""


@pytest.fixture(scope="module")
def rendered(tmp_path_factory):
    if not shutil.which("node"):
        pytest.skip("node 없음 — worker 페이지 렌더 검증 생략")
    harness = tmp_path_factory.mktemp("w") / "h.mjs"
    harness.write_text(_HARNESS)
    out = subprocess.run(["node", str(harness), str(WORKER)],
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def test_external_values_are_escaped_before_they_reach_html(rendered):
    """`out.error`는 Slack이 준 문자열이다 — escape 없이 박히면 주입 경로가 된다.
    CSP는 backstop이지 1차 방어가 아니다."""
    html = rendered["html"]
    assert "<img" not in html and "&lt;img" in html
    assert all(a != b for a, b in zip(rendered["escaped"], ["&", "<", ">", '"', "'"]))


def test_page_paints_its_own_background_in_both_schemes(rendered):
    """`color`만 있고 `background`가 없으면 캔버스를 UA에 맡기는 것이다 — 캔버스를
    스스로 어둡게 칠하는 UA(모바일 auto-dark·인앱 웹뷰)에서 검은 글씨가 된다."""
    css = rendered["css"]
    assert "background:var(--plane)" in css
    assert "--plane:#f9f9f7" in css                       # 라이트
    assert "prefers-color-scheme:dark" in css and "--plane:#0d0d0d" in css
    assert 'name="color-scheme"' in rendered["html"]


def test_page_is_self_contained_under_the_worker_csp(rendered):
    """CSP가 `default-src 'none'`이다 — 외부 요청이나 스크립트가 있으면 그냥 안 뜬다."""
    html = rendered["html"]
    assert "<script" not in html and "http://" not in html and "https://" not in html
    assert 'name="viewport"' in html                      # 없으면 모바일이 축소해 그린다
