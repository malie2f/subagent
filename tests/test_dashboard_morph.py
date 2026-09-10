"""仪表盘：Idiomorph 就地 morph + SSE 脏推送。"""
from pathlib import Path

from mcp_hub.dashboard.api import file_mtime_ns

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "mcp_hub" / "dashboard" / "static"
TMPL = ROOT / "mcp_hub" / "dashboard" / "templates" / "index.html"


def test_idiomorph_vendored_and_wired():
    lib = (STATIC / "idiomorph.min.js").read_text(encoding="utf-8")
    assert "Idiomorph" in lib
    assert "ignoreActiveValue" in lib
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "Idiomorph.morph" in app
    assert "function setHtml(" in app
    assert "ignoreActiveValue: true" in app
    assert "pollInFlight" in app
    html = TMPL.read_text(encoding="utf-8")
    assert "idiomorph.min.js" in html
    assert html.index("idiomorph.min.js") < html.index("app.js")
    assert "new EventSource('/api/events')" in app
    assert "function startLive(" in app


def test_file_mtime_ns_missing_and_touch(tmp_path: Path):
    p = tmp_path / "x.json"
    assert file_mtime_ns(p) == 0
    p.write_text("{}", encoding="utf-8")
    t1 = file_mtime_ns(p)
    assert t1 > 0
    p.write_text("{}\n", encoding="utf-8")
    assert file_mtime_ns(p) >= t1
