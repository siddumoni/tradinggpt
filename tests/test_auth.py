from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src.auth import (
    AuthConfigurationError,
    load_credentials,
    load_token_cache,
    save_token_cache,
    token_expiry_for,
)

IST = ZoneInfo("Asia/Kolkata")


def test_expiry_before_six_am_is_next_day() -> None:
    now = datetime(2026, 10, 17, 5, 30, tzinfo=IST)
    assert token_expiry_for(now) == datetime(2026, 10, 18, 6, 0, tzinfo=IST)


def test_expiry_after_six_am_is_next_day() -> None:
    now = datetime(2026, 10, 17, 9, 0, tzinfo=IST)
    assert token_expiry_for(now) == datetime(2026, 10, 18, 6, 0, tzinfo=IST)


def test_expiry_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        token_expiry_for(datetime(2026, 10, 17, 9, 0))


def test_save_and_load_token_cache(tmp_path: Path) -> None:
    cache = tmp_path / "token.json"
    now = datetime(2026, 10, 17, 9, 0, tzinfo=IST)
    save_token_cache("dummy-token", "dummy-key", cache_path=cache, now=now)

    loaded = load_token_cache(
        "dummy-key", cache_path=cache, now=datetime(2026, 10, 17, 10, 0, tzinfo=IST)
    )
    assert loaded is not None
    assert loaded.access_token == "dummy-token"
    assert loaded.expires_at == datetime(2026, 10, 18, 6, 0, tzinfo=IST)


def test_expired_token_cache_is_ignored(tmp_path: Path) -> None:
    cache = tmp_path / "token.json"
    save_token_cache(
        "dummy-token", "dummy-key", cache_path=cache,
        now=datetime(2026, 10, 17, 9, 0, tzinfo=IST),
    )
    loaded = load_token_cache(
        "dummy-key", cache_path=cache, now=datetime(2026, 10, 18, 6, 0, tzinfo=IST)
    )
    assert loaded is None


def test_cache_for_different_api_key_is_ignored(tmp_path: Path) -> None:
    cache = tmp_path / "token.json"
    save_token_cache(
        "dummy-token", "key-a", cache_path=cache,
        now=datetime(2026, 10, 17, 9, 0, tzinfo=IST),
    )
    assert load_token_cache(
        "key-b", cache_path=cache, now=datetime(2026, 10, 17, 10, 0, tzinfo=IST)
    ) is None


def test_malformed_cache_raises_clear_error(tmp_path: Path) -> None:
    cache = tmp_path / "token.json"
    cache.write_text("not-json", encoding="utf-8")
    from src.auth import TokenCacheError
    with pytest.raises(TokenCacheError, match="unreadable or malformed"):
        load_token_cache("key", cache_path=cache)


def test_credentials_require_non_placeholder_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "KITE_API_KEY=replace_with_your_api_key\nKITE_API_SECRET=replace_with_your_api_secret\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("KITE_API_KEY", raising=False)
    monkeypatch.delenv("KITE_API_SECRET", raising=False)
    with pytest.raises(AuthConfigurationError, match="Set KITE_API_KEY"):
        load_credentials(env_file)


def test_keyring_token_storage_keeps_access_token_out_of_json_cache(tmp_path, monkeypatch):
    import json
    import sys
    from types import SimpleNamespace

    secrets = {}

    def set_password(service, username, password):
        secrets[(service, username)] = password

    def get_password(service, username):
        return secrets.get((service, username))

    monkeypatch.setitem(
        sys.modules,
        "keyring",
        SimpleNamespace(set_password=set_password, get_password=get_password),
    )
    monkeypatch.setenv("TRADINGGPT_USE_KEYRING", "true")
    cache = tmp_path / "token.json"
    now = datetime(2026, 11, 6, 9, 0, tzinfo=IST)

    save_token_cache("secret-access-token", "dummy-key", cache_path=cache, now=now)
    payload = json.loads(cache.read_text(encoding="utf-8"))

    assert payload["token_storage"] == "keyring"
    assert "access_token" not in payload
    assert secrets[("tradinggpt.kite", "dummy-key")] == "secret-access-token"
    loaded = load_token_cache("dummy-key", cache_path=cache, now=now)
    assert loaded is not None
    assert loaded.access_token == "secret-access-token"


def test_keyring_mode_does_not_fall_back_to_plaintext_when_dependency_missing(tmp_path, monkeypatch):
    import sys
    from src.auth import TokenCacheError

    monkeypatch.setenv("TRADINGGPT_USE_KEYRING", "true")
    monkeypatch.delitem(sys.modules, "keyring", raising=False)
    # Force import failure even if a developer machine happens to have keyring installed.
    import builtins
    original_import = builtins.__import__

    def blocked_import(name, *args, **kwargs):
        if name == "keyring":
            raise ImportError("test missing optional dependency")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)
    with pytest.raises(TokenCacheError, match="keyring is not installed"):
        save_token_cache(
            "secret-access-token", "dummy-key", cache_path=tmp_path / "token.json",
            now=datetime(2026, 11, 6, 9, 0, tzinfo=IST),
        )
