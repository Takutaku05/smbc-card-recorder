import os
import sys
import types
from unittest.mock import MagicMock

try:
    import gspread  # noqa: F401
except ImportError:
    class _WorksheetNotFound(Exception):
        pass

    class _SpreadsheetNotFound(Exception):
        pass

    class _BackOffHTTPClient:
        pass

    sys.modules["gspread"] = types.SimpleNamespace(
        exceptions=types.SimpleNamespace(
            WorksheetNotFound=_WorksheetNotFound,
            SpreadsheetNotFound=_SpreadsheetNotFound,
        ),
        BackOffHTTPClient=_BackOffHTTPClient,
        service_account=MagicMock(),
    )

try:
    import schedule  # noqa: F401
except ImportError:
    sys.modules["schedule"] = types.SimpleNamespace()

import gspread as _gspread

from src import mailSystem


def test_resolve_config_path_normalizes_relative_paths():
    assert mailSystem._resolve_config_path("config/sa.json") == os.path.join(
        mailSystem.BASE_DIR, "config/sa.json"
    )
    assert mailSystem._resolve_config_path("/abs/path/sa.json") == "/abs/path/sa.json"
    assert mailSystem._resolve_config_path(None) is None


def test_initialize_services_fails_when_spreadsheet_id_missing(tmp_path, monkeypatch):
    token_file = tmp_path / "gmailtoken.json"
    token_file.write_text("{}", encoding="utf-8")
    creds = MagicMock(valid=True)

    monkeypatch.setattr(mailSystem, "TOKEN_FILE", str(token_file))
    monkeypatch.setattr(mailSystem, "SERVICE_ACCOUNT_FILE", "dummy_sa.json")
    monkeypatch.setattr(mailSystem, "SPREADSHEET_ID", "")
    monkeypatch.setattr(mailSystem, "GmailCredentials", types.SimpleNamespace(from_authorized_user_file=lambda *_a: creds))
    monkeypatch.setattr(mailSystem, "build", lambda *_a, **_kw: MagicMock())

    assert mailSystem.initialize_services() is False


def test_initialize_services_fails_and_notifies_on_spreadsheet_not_found(tmp_path, monkeypatch):
    token_file = tmp_path / "gmailtoken.json"
    token_file.write_text("{}", encoding="utf-8")
    creds = MagicMock(valid=True)
    gc_mock = MagicMock()
    gc_mock.open_by_key.side_effect = _gspread.exceptions.SpreadsheetNotFound("missing")
    discord_cls = MagicMock()

    monkeypatch.setattr(mailSystem, "TOKEN_FILE", str(token_file))
    monkeypatch.setattr(mailSystem, "SERVICE_ACCOUNT_FILE", "dummy_sa.json")
    monkeypatch.setattr(mailSystem, "SPREADSHEET_ID", "bad-id")
    monkeypatch.setattr(mailSystem, "GmailCredentials", types.SimpleNamespace(from_authorized_user_file=lambda *_a: creds))
    monkeypatch.setattr(mailSystem, "build", lambda *_a, **_kw: MagicMock())
    monkeypatch.setattr(_gspread, "service_account", lambda **_kw: gc_mock, raising=False)
    monkeypatch.setattr(mailSystem.mailDisco, "MailDisco", discord_cls)

    assert mailSystem.initialize_services() is False
    discord_cls.return_value.send.assert_called_once()


def test_initialize_services_succeeds_with_warning_when_discord_webhook_unset(tmp_path, monkeypatch):
    token_file = tmp_path / "gmailtoken.json"
    token_file.write_text("{}", encoding="utf-8")
    creds = MagicMock(valid=True)
    gc_mock = MagicMock()

    monkeypatch.setattr(mailSystem, "TOKEN_FILE", str(token_file))
    monkeypatch.setattr(mailSystem, "SERVICE_ACCOUNT_FILE", "dummy_sa.json")
    monkeypatch.setattr(mailSystem, "SPREADSHEET_ID", "valid-id")
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.setattr(mailSystem, "GmailCredentials", types.SimpleNamespace(from_authorized_user_file=lambda *_a: creds))
    monkeypatch.setattr(mailSystem, "build", lambda *_a, **_kw: MagicMock())
    monkeypatch.setattr(_gspread, "service_account", lambda **_kw: gc_mock, raising=False)

    assert mailSystem.initialize_services() is True
    gc_mock.open_by_key.assert_called_once_with("valid-id")
