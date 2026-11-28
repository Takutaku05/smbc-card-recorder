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
        )
    )

try:
    import schedule  # noqa: F401
except ImportError:
    sys.modules["schedule"] = types.SimpleNamespace()

from google.auth.exceptions import RefreshError

from src import mailSystem


def test_refresh_error_on_list_notifies_once_with_recovery_and_resets_after_success(monkeypatch):
    sent = []

    class _Recorder:
        def __init__(self, msg):
            self.msg = msg

        def send(self):
            sent.append(self.msg)

    service = MagicMock()
    service.users.return_value.messages.return_value.list.return_value.execute.side_effect = RefreshError("expired")
    save_run_mock = MagicMock()

    monkeypatch.setattr(mailSystem, "gc", MagicMock())
    monkeypatch.setattr(mailSystem, "gmail_service", service)
    monkeypatch.setattr(mailSystem, "get_last_run_time", lambda: None)
    monkeypatch.setattr(mailSystem, "save_current_run_time", save_run_mock)
    monkeypatch.setattr(mailSystem, "_refresh_error_notified", False)
    monkeypatch.setattr(mailSystem, "_gmail_creds", None)
    monkeypatch.setattr(mailSystem.mailDisco, "MailDisco", _Recorder)

    # 1回目・2回目の連続失効では1回だけ通知される
    mailSystem.check_mail_job()
    mailSystem.check_mail_job()
    assert len(sent) == 1
    assert "gmailtoken.json" in sent[0]
    save_run_mock.assert_not_called()

    # 正常実行後に再度失効した場合は再び通知される
    service.users.return_value.messages.return_value.list.return_value.execute.side_effect = [{"messages": []}]
    monkeypatch.setattr(mailSystem, "save_processed_ids", MagicMock())
    mailSystem.check_mail_job()
    assert mailSystem._refresh_error_notified is False

    service.users.return_value.messages.return_value.list.return_value.execute.side_effect = RefreshError("expired again")
    mailSystem.check_mail_job()
    assert len(sent) == 2


def test_refresh_error_on_message_get_escapes_loop_and_notifies(monkeypatch):
    sent = []

    class _Recorder:
        def __init__(self, msg):
            self.msg = msg

        def send(self):
            sent.append(self.msg)

    service = MagicMock()
    service.users.return_value.messages.return_value.list.return_value.execute.return_value = {
        "messages": [{"id": "m1"}]
    }
    service.users.return_value.messages.return_value.get.return_value.execute.side_effect = RefreshError("revoked")
    save_run_mock = MagicMock()

    monkeypatch.setattr(mailSystem, "gc", MagicMock())
    monkeypatch.setattr(mailSystem, "gmail_service", service)
    monkeypatch.setattr(mailSystem, "processed_ids", set())
    monkeypatch.setattr(mailSystem, "get_last_run_time", lambda: None)
    monkeypatch.setattr(mailSystem, "save_current_run_time", save_run_mock)
    monkeypatch.setattr(mailSystem, "_refresh_error_notified", False)
    monkeypatch.setattr(mailSystem, "_gmail_creds", None)
    monkeypatch.setattr(mailSystem.mailDisco, "MailDisco", _Recorder)

    mailSystem.check_mail_job()
    assert len(sent) == 1
    assert "gmailtoken.json" in sent[0]
    save_run_mock.assert_not_called()


def test_persist_refreshed_token_writes_when_token_json_changes(tmp_path, monkeypatch):
    token_file = tmp_path / "gmailtoken.json"
    creds = MagicMock()
    creds.to_json.return_value = '{"token": "refreshed-token"}'

    service = MagicMock()
    service.users.return_value.messages.return_value.list.return_value.execute.return_value = {"messages": []}

    monkeypatch.setattr(mailSystem, "TOKEN_FILE", str(token_file))
    monkeypatch.setattr(mailSystem, "gc", MagicMock())
    monkeypatch.setattr(mailSystem, "gmail_service", service)
    monkeypatch.setattr(mailSystem, "get_last_run_time", lambda: None)
    monkeypatch.setattr(mailSystem, "save_current_run_time", MagicMock())
    monkeypatch.setattr(mailSystem, "save_processed_ids", MagicMock())
    monkeypatch.setattr(mailSystem, "_gmail_creds", creds)
    monkeypatch.setattr(mailSystem, "_last_saved_token_json", '{"token": "old-token"}')

    mailSystem.check_mail_job()
    assert token_file.read_text(encoding="utf-8") == '{"token": "refreshed-token"}'
    assert mailSystem._last_saved_token_json == '{"token": "refreshed-token"}'
