"""Dashboard front end: template/JS consistency, served sections, and a JS render check (Node, no browser).

All data is synthetic (demo generator). The Node test is skipped when `node` is not installed.
"""

import json
import re
import shutil
import subprocess
import threading
import urllib.request
from datetime import date
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from yt_report.config import load_config
from yt_report.dashboard import WEB_ROOT, make_handler
from yt_report.demo import build_demo_reports
from yt_report.storage import Storage

TEMPLATE = (WEB_ROOT / "templates" / "index.html").read_text(encoding="utf-8")
APP_JS = WEB_ROOT / "static" / "app.js"
HARNESS = Path(__file__).parent / "js" / "dashboard_harness.js"


# ------------------------------------------------------------------ static checks ---
def test_every_element_id_used_by_app_js_exists_in_the_template():
    used = set(re.findall(r'\$\("([A-Za-z][\w-]*)"\)', APP_JS.read_text(encoding="utf-8")))
    defined = set(re.findall(r'\bid="([^"]+)"', TEMPLATE))
    assert used, "no element lookups found"
    assert used <= defined, f"app.js uses ids missing from index.html: {sorted(used - defined)}"


def test_section_navigation_targets_exist():
    targets = re.findall(r'<nav class="sections"[^>]*>(.*?)</nav>', TEMPLATE, re.S)[0]
    hrefs = re.findall(r'href="#([^"]+)"', targets)
    assert hrefs == ["overview", "formats", "trends", "videos", "dataQuality", "historySection"]
    defined = set(re.findall(r'\bid="([^"]+)"', TEMPLATE))
    assert set(hrefs) <= defined


def test_template_has_loading_empty_error_and_demo_states():
    for state in ('id="loading"', 'id="empty"', 'id="errorState"', 'id="demoBanner"'):
        assert state in TEMPLATE
    assert "DEMO" in TEMPLATE and "Synthetic data" in TEMPLATE


# ------------------------------------------------------------- served dashboard ---
@pytest.fixture
def demo_db(tmp_path, monkeypatch):
    for k in ("AI_RECOMMENDATIONS", "ANTHROPIC_API_KEY", "YT_TOKEN_JSON", "DISCORD_WEBHOOK_URL", "REPORT_WEEK_END"):
        monkeypatch.delenv(k, raising=False)
    env = tmp_path / ".env"
    env.write_text("LOG_DIR=\n")
    db = tmp_path / "demo.db"
    with Storage(db) as s:
        for r in build_demo_reports(load_config(env), weeks=4, today=date(2026, 11, 10)):
            s.save_report(r)
    return db


def _serve(db, mode="demo"):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(db, mode=mode))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def test_dashboard_serves_page_assets_and_v2_fields(demo_db):
    httpd, url = _serve(demo_db)
    try:
        page = urllib.request.urlopen(url + "/", timeout=5).read().decode()
        assert 'id="sectionNav"' in page and 'id="videoTable"' in page and 'id="coverage"' in page
        for asset in ("/static/app.js", "/static/styles.css"):
            assert urllib.request.urlopen(url + asset, timeout=5).status == 200
        data = json.loads(urllib.request.urlopen(url + "/api/data", timeout=5).read())
    finally:
        httpd.shutdown()
        httpd.server_close()
    latest = data["reports"][-1]
    assert data["mode"] == "demo" and len(data["reports"]) == 4
    assert latest["coverage"]["current"]["complete"] == 1 and latest["views_by_age"] is not None
    assert latest["videos"] and all("views" in v and "content_type" in v for v in latest["videos"])


# ------------------------------------------------------------- JS render (Node) ---
NODE = shutil.which("node")


def _render(tmp_path, payload):
    if payload == "FETCH_FAIL":
        arg = payload
    else:
        arg = str(tmp_path / "data.json")
        Path(arg).write_text(json.dumps(payload), encoding="utf-8")
    out = subprocess.run([NODE, str(HARNESS), str(APP_JS), arg], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_js_renders_demo_reports_without_errors(tmp_path, demo_db):
    with Storage(demo_db) as s:
        data = s.dashboard_data()
    data["mode"] = "demo"
    res = _render(tmp_path, data)
    els = res["els"]
    assert res["errors"] == []
    assert els["loading"]["hidden"] and not els["content"]["hidden"] and els["empty"]["hidden"]
    assert not els["demoBanner"]["hidden"] and res["title"].startswith("DEMO")
    assert not els["sectionNav"]["hidden"] and not els["reportSelect"]["disabled"]
    assert els["kpis"]["html"].count('class="kpi"') == 7
    assert els["videoTable"]["html"].count("<tr>") == 1 + 10          # header + first page of 10 videos
    assert "Show all" in els["videoMore"]["text"] and not els["videoMore"]["hidden"]
    assert "All formats" in els["videoFilter"]["html"]
    assert "Complete" in els["coverage"]["html"] and "100.0%" in els["coverage"]["html"]
    assert "Older catalog" in els["viewsByAge"]["html"]
    assert els["history"]["html"].count("<tr>") == 1 + 4


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_js_shows_na_for_snapshots_without_v2_fields(tmp_path, demo_db):
    with Storage(demo_db) as s:
        data = s.dashboard_data()
    for r in data["reports"]:
        r["coverage"] = r["publications"] = r["views_by_age"] = None    # as stored by v1.1
    data["mode"] = "production"
    res = _render(tmp_path, data)
    assert res["errors"] == []
    assert "Not measured" in res["els"]["coverage"]["html"] and "Not measured" in res["els"]["viewsByAge"]["html"]
    assert res["els"]["demoBanner"]["hidden"]


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_js_empty_and_error_states(tmp_path):
    empty = _render(tmp_path, {"reports": [], "daily": [], "mode": "production"})
    assert empty["errors"] == [] and not empty["els"]["empty"]["hidden"] and empty["els"]["loading"]["hidden"]
    failed = _render(tmp_path, "FETCH_FAIL")
    assert not failed["els"]["errorState"]["hidden"] and failed["els"]["loading"]["hidden"]
    assert "could not reach its local server" in failed["els"]["errorText"]["text"]
