import json
from datetime import datetime, timedelta, timezone

import pytest

from yt_report import auth
from yt_report.config import load_config


def _token(scopes, expiry_delta=timedelta(hours=1)):
    expiry = (datetime.now(timezone.utc) + expiry_delta).replace(tzinfo=None).isoformat() + "Z"
    return json.dumps({"token": "fake-access", "refresh_token": "fake-refresh", "client_id": "cid",
                       "client_secret": "csecret", "token_uri": "https://oauth2.googleapis.com/token",
                       "scopes": scopes, "expiry": expiry})


@pytest.fixture
def cfg_with_token(tmp_path, monkeypatch):
    monkeypatch.delenv("YT_TOKEN_JSON", raising=False)

    def make(content):
        (tmp_path / "token.json").write_text(content)
        env = tmp_path / ".env"
        env.write_text("YT_TOKEN_FILE=token.json\n")
        return load_config(env)
    return make


def test_valid_token_is_used_without_network(cfg_with_token):
    creds = auth.get_credentials(cfg_with_token(_token(auth.SCOPES)))
    assert creds.valid


def test_missing_scope_requires_reauth(cfg_with_token):
    with pytest.raises(auth.AuthError):
        auth.get_credentials(cfg_with_token(_token(auth.SCOPES[:1])))


def test_corrupt_token_requires_reauth(cfg_with_token):
    with pytest.raises(auth.AuthError):
        auth.get_credentials(cfg_with_token("{not json"))


def test_token_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("YT_TOKEN_JSON", _token(auth.SCOPES))
    env = tmp_path / ".env"
    env.write_text("")
    assert auth.get_credentials(load_config(env)).valid
