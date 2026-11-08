"""Release hardening: dashboard Host check, server start errors, CLI/config bounds, token file mode.

No network beyond 127.0.0.1, no credentials; all data is synthetic.
"""

import http.client
import os
import socket
import stat
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from yt_report import __main__ as cli
from yt_report.auth import _save_token
from yt_report.config import ConfigError, load_config
from yt_report.dashboard import DashboardStartError, _host_name, make_handler, serve


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k in ("DASHBOARD_PORT", "DASHBOARD_HOST", "YT_TOKEN_JSON", "DISCORD_WEBHOOK_URL", "REPORT_WEEK_END"):
        monkeypatch.delenv(k, raising=False)
    p = tmp_path / ".env"
    p.write_text("LOG_DIR=\n")
    return p


def _get(port, host_header, path="/api/data"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", path, headers={"Host": host_header})
    r = c.getresponse()
    return r.status, dict(r.getheaders()), r.read()


@pytest.fixture
def server(tmp_path):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(tmp_path / "none.db", mode="demo"))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


@pytest.mark.parametrize("host", ["localhost:{p}", "127.0.0.1:{p}", "LOCALHOST:{p}", "localhost"])
def test_loopback_dashboard_accepts_local_host_names(server, host):
    status, headers, body = _get(server, host.format(p=server))
    assert status == 200 and b'"reports"' in body
    assert headers["X-Frame-Options"] == "DENY" and headers["X-Content-Type-Options"] == "nosniff"


@pytest.mark.parametrize("host", ["evil.example:{p}", "evil.example", "127.0.0.1.evil.example:{p}", ""])
def test_loopback_dashboard_rejects_foreign_host_header(server, host):
    """DNS rebinding: a foreign site resolving to 127.0.0.1 must not be able to read the analytics."""
    for path in ("/api/data", "/", "/static/app.js"):
        status, _, body = _get(server, host.format(p=server), path)
        assert status == 403 and b"reports" not in body


def test_host_name_parsing():
    assert _host_name("localhost:8050") == "localhost"
    assert _host_name("[::1]:8050") == "[::1]"
    assert _host_name("::1") == "::1"
    assert _host_name(None) == ""


def test_port_in_use_is_a_clean_error(env, tmp_path):
    cfg = load_config(env)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        port = s.getsockname()[1]
        with pytest.raises(DashboardStartError, match="could not listen"):
            serve(cfg, "127.0.0.1", port, open_browser=False, db_path=tmp_path / "x.db")
        assert cli.main(["--env-file", str(env), "dashboard", "--no-browser", "--port", str(port)]) == 2


def test_second_dashboard_on_the_same_port_fails_instead_of_starting_silently(env, tmp_path):
    """Windows: plain SO_REUSEADDR would let the second server bind while the first keeps answering."""
    cfg = load_config(env)
    first = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(tmp_path / "x.db"))  # e.g. an older instance
    try:
        port = first.server_address[1]
        with pytest.raises(DashboardStartError, match="could not listen"):
            serve(cfg, "127.0.0.1", port, open_browser=False, db_path=tmp_path / "x.db")
    finally:
        first.server_close()


@pytest.mark.parametrize("value", ["0", "65536", "70000"])
def test_dashboard_port_env_out_of_range_is_a_config_error(env, monkeypatch, value):
    monkeypatch.setenv("DASHBOARD_PORT", value)
    with pytest.raises(ConfigError, match="DASHBOARD_PORT"):
        load_config(env)


@pytest.mark.parametrize("argv", [["dashboard", "--port", "70000"], ["demo", "--port", "0"],
                                  ["demo", "--weeks", "0"], ["demo", "--weeks", "-3"], ["demo", "--weeks", "x"]])
def test_cli_rejects_out_of_range_numbers(env, argv, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--env-file", str(env), *argv])
    assert exc.value.code == 2
    assert "must be" in capsys.readouterr().err


def test_demo_with_one_week_prints_summary(env, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["--env-file", str(env), "demo", "--weeks", "1", "--print", "--no-dashboard"]) == 0
    assert "DEMO MODE" in capsys.readouterr().out


@pytest.mark.parametrize("content", [b"not a database at all, just text" * 4, None])
def test_failed_storage_open_releases_the_file(tmp_path, content):
    """Corrupt / too-new databases raise, but the file handle is closed (replaceable on Windows)."""
    import sqlite3

    from yt_report.storage import Storage

    db = tmp_path / "x.db"
    if content is None:  # schema newer than this program
        with sqlite3.connect(db) as c:
            c.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
            c.execute("INSERT INTO meta VALUES ('schema_version', '99')")
        c.close()
    else:
        db.write_bytes(content)
    with pytest.raises(sqlite3.DatabaseError):
        Storage(db)
    replacement = tmp_path / "new.db"
    replacement.write_bytes(b"")
    os.replace(replacement, db)  # fails with PermissionError on Windows if a handle leaked
    db.unlink()


def test_dashboard_reports_corrupt_database_and_recovers(tmp_path):
    db = tmp_path / "x.db"
    db.write_bytes(b"not a database at all, just text" * 4)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(db))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    try:
        status, _, body = _get(port, f"127.0.0.1:{port}")
        assert status == 500 and b"could not be read" in body
        db.unlink()  # restoring a backup must be possible while the dashboard runs
        status, _, body = _get(port, f"127.0.0.1:{port}")
        assert status == 200 and b'"reports": []' in body
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_demo_on_corrupt_demo_database_exits_7_without_traceback(env, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    demo_db = tmp_path / "data" / "demo" / "demo.db"
    demo_db.parent.mkdir(parents=True)
    demo_db.write_bytes(b"not a database at all, just text" * 4)
    assert cli.main(["--env-file", str(env), "demo", "--no-dashboard"]) == 7
    err = capsys.readouterr().err
    assert "Demo database could not be written" in err and "Traceback" not in err
    assert demo_db.read_bytes().startswith(b"not a database")  # not silently replaced


class _FakeCreds:
    def to_json(self):
        return '{"token": "synthetic", "refresh_token": "synthetic"}'


def test_token_file_written_atomically(tmp_path):
    path = tmp_path / "sub" / "token.json"
    _save_token(_FakeCreds(), path)
    assert path.read_text(encoding="utf-8").startswith('{"token"')
    assert not (tmp_path / "sub" / "token.json.tmp").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_token_file_is_owner_only_even_if_stale_tmp_exists(tmp_path):
    path = tmp_path / "token.json"
    stale = Path(str(path) + ".tmp")
    stale.write_text("old")
    stale.chmod(0o644)
    _save_token(_FakeCreds(), path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
