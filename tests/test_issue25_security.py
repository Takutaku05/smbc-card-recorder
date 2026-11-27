import stat
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

    sys.modules["gspread"] = types.SimpleNamespace(
        exceptions=types.SimpleNamespace(
            WorksheetNotFound=_WorksheetNotFound,
            SpreadsheetNotFound=_SpreadsheetNotFound,
        ),
        service_account=MagicMock(),
    )

try:
    import schedule  # noqa: F401
except ImportError:
    sys.modules["schedule"] = types.SimpleNamespace()

from src import logger_config, mailSystem


def test_scopes_sheet_excludes_drive_scope():
    assert "https://www.googleapis.com/auth/spreadsheets" in mailSystem.SCOPES_SHEET
    assert "https://www.googleapis.com/auth/drive" not in mailSystem.SCOPES_SHEET


def test_write_token_atomic_sets_restricted_permissions(tmp_path):
    config_dir = tmp_path / "config"
    token_file = config_dir / "gmailtoken.json"

    mailSystem._write_token_atomic(str(token_file), '{"token": "abc"}')

    assert token_file.read_text(encoding="utf-8") == '{"token": "abc"}'
    assert not (config_dir / "gmailtoken.json.tmp").exists()
    assert stat.S_IMODE(config_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600


def test_corrupted_token_file_falls_back_to_reauth_instead_of_crashing(tmp_path, monkeypatch):
    token_file = tmp_path / "config" / "gmailtoken.json"
    token_file.parent.mkdir(mode=0o700)
    token_file.write_text("{broken json", encoding="utf-8")

    monkeypatch.setattr(mailSystem, "TOKEN_FILE", str(token_file))
    # credentials.json が存在しないため FileNotFoundError 経路で False を返せば、
    # from_authorized_user_file の ValueError を未捕捉で落とさず復旧フローに入れたことを確認できる
    monkeypatch.setattr(
        mailSystem.os.path,
        "abspath",
        lambda _p: str(tmp_path / "src" / "mailSystem.py"),
    )

    assert mailSystem.initialize_services() is False


def test_restricted_timed_rotating_file_handler_uses_0600(tmp_path):
    log_path = tmp_path / "app.log"
    handler = logger_config._RestrictedTimedRotatingFileHandler(
        filename=str(log_path),
        when="midnight",
        interval=1,
        backupCount=7,
        encoding="utf-8",
    )
    try:
        assert stat.S_IMODE(log_path.stat().st_mode) == 0o600
    finally:
        handler.close()
