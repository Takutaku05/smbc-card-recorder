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

from googleapiclient.errors import HttpError

from src import mailSystem


def _make_gmail_service(message_ids, get_side_effect):
    service = MagicMock()
    service.users.return_value.messages.return_value.list.return_value.execute.return_value = {
        "messages": [{"id": mid} for mid in message_ids]
    }
    service.users.return_value.messages.return_value.get.return_value.execute.side_effect = get_side_effect
    return service


def test_save_current_run_time_called_when_all_messages_succeed(monkeypatch):
    payload = {"payload": {"headers": [{"name": "Subject", "value": "test"}]}}
    save_run_mock = MagicMock()
    save_ids_mock = MagicMock()

    monkeypatch.setattr(mailSystem, "gc", MagicMock())
    monkeypatch.setattr(mailSystem, "gmail_service", _make_gmail_service(["m1"], [payload]))
    monkeypatch.setattr(mailSystem, "processed_ids", set())
    monkeypatch.setattr(mailSystem, "get_last_run_time", lambda: None)
    monkeypatch.setattr(mailSystem, "save_current_run_time", save_run_mock)
    monkeypatch.setattr(mailSystem, "save_processed_ids", save_ids_mock)
    monkeypatch.setattr(mailSystem, "get_email_body", lambda _p: "body")
    monkeypatch.setattr(mailSystem, "system", lambda _b: True)
    monkeypatch.setattr(mailSystem.mailDisco, "MailDisco", MagicMock())

    mailSystem.check_mail_job()
    assert "m1" in mailSystem.processed_ids
    save_run_mock.assert_called_once()
    save_ids_mock.assert_called_once()


def test_save_current_run_time_called_when_no_messages(monkeypatch):
    save_run_mock = MagicMock()
    save_ids_mock = MagicMock()

    monkeypatch.setattr(mailSystem, "gc", MagicMock())
    monkeypatch.setattr(mailSystem, "gmail_service", _make_gmail_service([], []))
    monkeypatch.setattr(mailSystem, "processed_ids", set())
    monkeypatch.setattr(mailSystem, "get_last_run_time", lambda: None)
    monkeypatch.setattr(mailSystem, "save_current_run_time", save_run_mock)
    monkeypatch.setattr(mailSystem, "save_processed_ids", save_ids_mock)

    mailSystem.check_mail_job()
    save_run_mock.assert_called_once()
    save_ids_mock.assert_called_once()


def test_save_current_run_time_not_called_when_partial_write_fails(monkeypatch):
    p1 = {"payload": {"headers": [{"name": "Subject", "value": "ok"}]}}
    p2 = {"payload": {"headers": [{"name": "Subject", "value": "ng"}]}}
    save_run_mock = MagicMock()
    save_ids_mock = MagicMock()

    service = MagicMock()
    service.users.return_value.messages.return_value.list.return_value.execute.return_value = {
        "messages": [{"id": "m1"}, {"id": "m2"}]
    }
    payload_by_id = {"m1": p1, "m2": p2}
    service.users.return_value.messages.return_value.get.side_effect = (
        lambda **kwargs: types.SimpleNamespace(execute=lambda **_kw: payload_by_id[kwargs["id"]])
    )

    monkeypatch.setattr(mailSystem, "gc", MagicMock())
    monkeypatch.setattr(mailSystem, "gmail_service", service)
    monkeypatch.setattr(mailSystem, "processed_ids", set())
    monkeypatch.setattr(mailSystem, "get_last_run_time", lambda: None)
    monkeypatch.setattr(mailSystem, "save_current_run_time", save_run_mock)
    monkeypatch.setattr(mailSystem, "save_processed_ids", save_ids_mock)
    monkeypatch.setattr(mailSystem, "get_email_body", lambda p: p["headers"][0]["value"])
    monkeypatch.setattr(mailSystem, "system", lambda body: body == "ok")
    monkeypatch.setattr(mailSystem.mailDisco, "MailDisco", MagicMock())

    mailSystem.check_mail_job()
    assert mailSystem.processed_ids == {"m1"}
    save_run_mock.assert_not_called()
    save_ids_mock.assert_called_once()


def test_save_current_run_time_not_called_when_body_empty(monkeypatch):
    p1 = {"payload": {"headers": [{"name": "Subject", "value": "empty"}]}}
    save_run_mock = MagicMock()

    monkeypatch.setattr(mailSystem, "gc", MagicMock())
    monkeypatch.setattr(mailSystem, "gmail_service", _make_gmail_service(["m1"], [p1]))
    monkeypatch.setattr(mailSystem, "processed_ids", set())
    monkeypatch.setattr(mailSystem, "get_last_run_time", lambda: None)
    monkeypatch.setattr(mailSystem, "save_current_run_time", save_run_mock)
    monkeypatch.setattr(mailSystem, "save_processed_ids", MagicMock())
    monkeypatch.setattr(mailSystem, "get_email_body", lambda _p: "")
    monkeypatch.setattr(mailSystem.mailDisco, "MailDisco", MagicMock())

    mailSystem.check_mail_job()
    assert mailSystem.processed_ids == set()
    save_run_mock.assert_not_called()


def test_save_current_run_time_not_called_on_message_http_error_and_notifies(monkeypatch):
    resp = types.SimpleNamespace(status=503, reason="Service Unavailable")
    http_err = HttpError(resp, b"error")
    save_run_mock = MagicMock()
    discord_cls = MagicMock()

    monkeypatch.setattr(mailSystem, "gc", MagicMock())
    monkeypatch.setattr(mailSystem, "gmail_service", _make_gmail_service(["m1"], [http_err]))
    monkeypatch.setattr(mailSystem, "processed_ids", set())
    monkeypatch.setattr(mailSystem, "get_last_run_time", lambda: None)
    monkeypatch.setattr(mailSystem, "save_current_run_time", save_run_mock)
    monkeypatch.setattr(mailSystem, "save_processed_ids", MagicMock())
    monkeypatch.setattr(mailSystem.mailDisco, "MailDisco", discord_cls)

    mailSystem.check_mail_job()
    save_run_mock.assert_not_called()
    discord_cls.return_value.send.assert_called_once()


def test_save_current_run_time_not_called_on_message_unexpected_error_and_notifies(monkeypatch):
    save_run_mock = MagicMock()
    discord_cls = MagicMock()

    monkeypatch.setattr(mailSystem, "gc", MagicMock())
    monkeypatch.setattr(
        mailSystem,
        "gmail_service",
        _make_gmail_service(["m1"], [RuntimeError("decode failed")]),
    )
    monkeypatch.setattr(mailSystem, "processed_ids", set())
    monkeypatch.setattr(mailSystem, "get_last_run_time", lambda: None)
    monkeypatch.setattr(mailSystem, "save_current_run_time", save_run_mock)
    monkeypatch.setattr(mailSystem, "save_processed_ids", MagicMock())
    monkeypatch.setattr(mailSystem.mailDisco, "MailDisco", discord_cls)

    mailSystem.check_mail_job()
    save_run_mock.assert_not_called()
    discord_cls.return_value.send.assert_called_once()
