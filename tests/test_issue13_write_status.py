import sys
import types
from unittest.mock import MagicMock

import pytest

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

import gspread as _gspread

from src import mailSystem


class _DummyPurpose:
    def __init__(self, location):
        self.location = location

    def judge(self):
        return "食費"


def _make_gmail_service(payload):
    service = MagicMock()
    service.users.return_value.messages.return_value.list.return_value.execute.return_value = {
        "messages": [{"id": "msg-1"}]
    }
    service.users.return_value.messages.return_value.get.return_value.execute.return_value = {
        "payload": payload
    }
    return service


def test_sheet_returns_false_when_gc_is_none(monkeypatch):
    monkeypatch.setattr(mailSystem, "gc", None)
    assert mailSystem.sheet("2026/01/05 12:00", "テスト店", 1000) is False


def test_sheet_returns_false_when_worksheets_missing(monkeypatch):
    spreadsheet = MagicMock()
    spreadsheet.worksheet.side_effect = _gspread.exceptions.WorksheetNotFound("missing")
    gc = MagicMock()
    gc.open_by_key.return_value = spreadsheet
    monkeypatch.setattr(mailSystem, "gc", gc)
    monkeypatch.setattr(mailSystem.mailDisco, "MailDisco", MagicMock())

    assert mailSystem.sheet("2026/01/05 12:00", "テスト店", 1000) is False


def test_sheet_returns_false_on_spreadsheet_not_found(monkeypatch):
    gc = MagicMock()
    gc.open_by_key.side_effect = _gspread.exceptions.SpreadsheetNotFound("not found")
    monkeypatch.setattr(mailSystem, "gc", gc)
    monkeypatch.setattr(mailSystem.mailDisco, "MailDisco", MagicMock())

    assert mailSystem.sheet("2026/01/05 12:00", "テスト店", 1000) is False


def test_sheet_returns_false_when_update_raises(monkeypatch):
    worksheet = MagicMock()
    worksheet.get_all_values.return_value = [["H1"], ["H2"], ["", "", "", ""]]
    worksheet.update.side_effect = RuntimeError("429 quota exceeded")
    spreadsheet = MagicMock()
    spreadsheet.worksheet.return_value = worksheet
    gc = MagicMock()
    gc.open_by_key.return_value = spreadsheet

    monkeypatch.setattr(mailSystem, "gc", gc)
    monkeypatch.setattr(mailSystem.purpose, "Purpose", _DummyPurpose)
    monkeypatch.setattr(mailSystem.mailDisco, "MailDisco", MagicMock())

    assert mailSystem.sheet("2026/01/05 12:00", "テスト店", 1000) is False


@pytest.mark.parametrize(
    "all_values,expected_method",
    [
        ([["H1"], ["H2"], ["", "", "", ""]], "update"),
        ([], "append_row"),
        ([["H1"], ["H2"], ["2026/01/01 10:00", "既存店", 500, "食費"]], "append_row"),
    ],
)
def test_sheet_returns_true_on_successful_write(monkeypatch, all_values, expected_method):
    worksheet = MagicMock()
    worksheet.get_all_values.return_value = all_values
    spreadsheet = MagicMock()
    spreadsheet.worksheet.return_value = worksheet
    gc = MagicMock()
    gc.open_by_key.return_value = spreadsheet

    monkeypatch.setattr(mailSystem, "gc", gc)
    monkeypatch.setattr(mailSystem.purpose, "Purpose", _DummyPurpose)
    monkeypatch.setattr(mailSystem.mailDisco, "MailDisco", MagicMock())

    assert mailSystem.sheet("2026/01/05 12:00", "テスト店", 1000) is True
    getattr(worksheet, expected_method).assert_called_once()


def test_sheet_returns_true_when_get_all_values_fails_and_append_succeeds(monkeypatch):
    worksheet = MagicMock()
    worksheet.get_all_values.side_effect = RuntimeError("temp read error")
    spreadsheet = MagicMock()
    spreadsheet.worksheet.return_value = worksheet
    gc = MagicMock()
    gc.open_by_key.return_value = spreadsheet

    monkeypatch.setattr(mailSystem, "gc", gc)
    monkeypatch.setattr(mailSystem.purpose, "Purpose", _DummyPurpose)
    monkeypatch.setattr(mailSystem.mailDisco, "MailDisco", MagicMock())

    assert mailSystem.sheet("2026/01/05 12:00", "テスト店", 1000) is True
    worksheet.append_row.assert_called_once()


def test_system_propagates_sheet_result(monkeypatch):
    monkeypatch.setattr(mailSystem, "sheet", lambda *_args: True)
    assert mailSystem.system("ご利用日時：2026/01/05 12:00\nご利用先：テスト店\nご利用金額：1,000円") is True

    monkeypatch.setattr(mailSystem, "sheet", lambda *_args: False)
    assert mailSystem.system("ご利用日時：2026/01/05 12:00\nご利用先：テスト店\nご利用金額：1,000円") is False


def test_check_mail_job_marks_processed_only_when_system_succeeds(monkeypatch):
    payload = {"headers": [{"name": "Subject", "value": "ご利用のお知らせ【三井住友カード】"}]}
    monkeypatch.setattr(mailSystem, "gc", MagicMock())
    monkeypatch.setattr(mailSystem, "gmail_service", _make_gmail_service(payload))
    monkeypatch.setattr(mailSystem, "processed_ids", set())
    monkeypatch.setattr(mailSystem, "get_last_run_time", lambda: None)
    monkeypatch.setattr(mailSystem, "save_current_run_time", MagicMock())
    monkeypatch.setattr(mailSystem, "save_processed_ids", MagicMock())
    monkeypatch.setattr(mailSystem, "get_email_body", lambda _p: "dummy body")
    monkeypatch.setattr(mailSystem, "system", lambda _b: True)
    monkeypatch.setattr(mailSystem.mailDisco, "MailDisco", MagicMock())

    mailSystem.check_mail_job()
    assert "msg-1" in mailSystem.processed_ids


def test_check_mail_job_does_not_mark_processed_when_system_fails(monkeypatch):
    payload = {"headers": [{"name": "Subject", "value": "ご利用のお知らせ【三井住友カード】"}]}
    discord_cls = MagicMock()
    monkeypatch.setattr(mailSystem, "gc", MagicMock())
    monkeypatch.setattr(mailSystem, "gmail_service", _make_gmail_service(payload))
    monkeypatch.setattr(mailSystem, "processed_ids", set())
    monkeypatch.setattr(mailSystem, "get_last_run_time", lambda: None)
    monkeypatch.setattr(mailSystem, "save_current_run_time", MagicMock())
    monkeypatch.setattr(mailSystem, "save_processed_ids", MagicMock())
    monkeypatch.setattr(mailSystem, "get_email_body", lambda _p: "dummy body")
    monkeypatch.setattr(mailSystem, "system", lambda _b: False)
    monkeypatch.setattr(mailSystem.mailDisco, "MailDisco", discord_cls)

    mailSystem.check_mail_job()
    assert "msg-1" not in mailSystem.processed_ids
    # 書き込み失敗の詳細（利用先・金額・エラー）は sheet() 内で通知済みのため、check_mail_job では重複通知しない
    discord_cls.return_value.send.assert_not_called()
    assert mailSystem._message_failure_counts == {"msg-1": 1}


def test_check_mail_job_does_not_mark_processed_when_body_empty(monkeypatch):
    payload = {"headers": [{"name": "Subject", "value": "ご利用のお知らせ【三井住友カード】"}]}
    discord_cls = MagicMock()
    monkeypatch.setattr(mailSystem, "gc", MagicMock())
    monkeypatch.setattr(mailSystem, "gmail_service", _make_gmail_service(payload))
    monkeypatch.setattr(mailSystem, "processed_ids", set())
    monkeypatch.setattr(mailSystem, "get_last_run_time", lambda: None)
    monkeypatch.setattr(mailSystem, "save_current_run_time", MagicMock())
    monkeypatch.setattr(mailSystem, "save_processed_ids", MagicMock())
    monkeypatch.setattr(mailSystem, "get_email_body", lambda _p: "")
    monkeypatch.setattr(mailSystem.mailDisco, "MailDisco", discord_cls)

    mailSystem.check_mail_job()
    assert "msg-1" not in mailSystem.processed_ids
    discord_cls.return_value.send.assert_called_once()
