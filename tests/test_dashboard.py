import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from yt_report.dashboard import make_handler
from yt_report.storage import Storage


@pytest.fixture
def server(tmp_path, report):
    db = tmp_path / "d.db"
    with Storage(db) as s:
        s.save_report(report)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(db))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, r.headers.get("Content-Type"), r.read()


def test_index_and_static(server):
    status, ctype, body = get(server + "/")
    assert status == 200 and "text/html" in ctype and b"app.js" in body
    assert get(server + "/static/app.js")[0] == 200
    assert get(server + "/static/styles.css")[0] == 200


def test_api_returns_stored_data(server):
    _, ctype, body = get(server + "/api/data")
    data = json.loads(body)
    assert ctype == "application/json"
    assert data["reports"][0]["totals"]["current"]["views"] == 4125


@pytest.mark.parametrize("path", ["/static/../../storage.py", "/static/..%2f..%2fstorage.py", "/nope"])
def test_path_traversal_blocked(server, path):
    with pytest.raises(urllib.error.HTTPError) as exc:
        get(server + path)
    assert exc.value.code == 404
