"""POC 스크립트 공용 — 스토어 초기화와 검증 결과 기록."""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

POC_DIR = Path(__file__).parent / "_artifacts"
POC_DIR.mkdir(exist_ok=True)


def stores():
    from devcrew.store.registry import SessionRegistry
    from devcrew.store.trace import TraceStore
    return TraceStore(POC_DIR / "trace.db"), SessionRegistry(POC_DIR / "harness.db")


def record(poc_id: str, results: dict[str, bool], extra: dict | None = None):
    ok = all(results.values())
    out = {"poc": poc_id, "ts": time.time(), "pass": ok,
           "checks": results, "extra": extra or {}}
    path = POC_DIR / f"{poc_id}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    for name, passed in results.items():
        print(("  ✅" if passed else "  ❌"), name)
    print(("PASS" if ok else "FAIL"), "→", path)
    if not ok:
        sys.exit(1)
