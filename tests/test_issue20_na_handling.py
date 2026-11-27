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

from src import mailSystem


class _DummyPurpose:
    def __init__(self, location):
        self.location = location

    def judge(self):
        return "食費"


def _setup_sheet_mocks(monkeypatch):
    sent = []

    class _Recorder:
        def __init__(self, msg):
            self.msg = msg

        def send(self):
            sent.append(self.msg)

    worksheet = MagicMock()
    worksheet.get_all_values.return_value = [["H1"], ["H2"], ["", "", "", ""]]
    spreadsheet = MagicMock()
    spreadsheet.worksheet.return_value = worksheet
    gc = MagicMock()
    gc.open_by_key.return_value = spreadsheet

    monkeypatch.setattr(mailSystem, "gc", gc)
    monkeypatch.setattr(mailSystem.purpose, "Purpose", _DummyPurpose)
    monkeypatch.setattr(mailSystem.mailDisco, "MailDisco", _Recorder)
    return worksheet, sent


def test_all_three_fields_missing_sends_single_consolidated_notification_and_skips_write(monkeypatch):
    worksheet, sent = _setup_sheet_mocks(monkeypatch)

    assert mailSystem.sheet("N/A", "N/A", "N/A") == mailSystem.SHEET_SKIPPED
    worksheet.update.assert_not_called()
    worksheet.append_row.assert_not_called()
    assert len(sent) == 1
    assert "利用日" in sent[0]
    assert "利用先" in sent[0]
    assert "利用金額" in sent[0]


def test_two_fields_missing_notifies_both_and_skips_write(monkeypatch):
    worksheet, sent = _setup_sheet_mocks(monkeypatch)

    assert mailSystem.sheet("2026/01/05 12:00", "N/A", "N/A") == mailSystem.SHEET_SKIPPED
    worksheet.update.assert_not_called()
    worksheet.append_row.assert_not_called()
    assert len(sent) == 1
    first_line = sent[0].splitlines()[0]
    assert "利用日" not in first_line
    assert "利用先" in first_line
    assert "利用金額" in first_line


def test_non_int_money_string_is_rejected_without_writing(monkeypatch):
    worksheet, sent = _setup_sheet_mocks(monkeypatch)

    assert mailSystem.sheet("2026/01/05 12:00", "テスト店", "不正文字列") == mailSystem.SHEET_SKIPPED
    worksheet.update.assert_not_called()
    worksheet.append_row.assert_not_called()
    assert len(sent) == 1
    assert "利用金額" in sent[0]


def test_complete_fields_write_normally(monkeypatch):
    worksheet, sent = _setup_sheet_mocks(monkeypatch)

    assert mailSystem.sheet("2026/01/05 12:00", "テスト店", 1200) is True
    worksheet.update.assert_called_once()
    assert len(sent) == 1
    assert "【正常に書き込みました】" in sent[0]


def test_check_mail_job_marks_skipped_message_processed(monkeypatch):
    """抽出失敗(SHEET_SKIPPED)は処理済みにし、last_run_time も進めて永久リトライを防ぐ。"""
    from unittest.mock import MagicMock

    service = MagicMock()
    service.users.return_value.messages.return_value.list.return_value.execute.return_value = {
        "messages": [{"id": "m1"}]
    }
    service.users.return_value.messages.return_value.get.return_value.execute.return_value = {
        "payload": {"headers": [{"name": "Subject", "value": "test"}]}
    }
    save_run_mock = MagicMock()

    monkeypatch.setattr(mailSystem, "gc", MagicMock())
    monkeypatch.setattr(mailSystem, "gmail_service", service)
    monkeypatch.setattr(mailSystem, "processed_ids", set())
    monkeypatch.setattr(mailSystem, "get_last_run_time", lambda: None)
    monkeypatch.setattr(mailSystem, "save_current_run_time", save_run_mock)
    monkeypatch.setattr(mailSystem, "save_processed_ids", MagicMock())
    monkeypatch.setattr(mailSystem, "get_email_body", lambda _p: "body")
    monkeypatch.setattr(mailSystem, "system", lambda _b: mailSystem.SHEET_SKIPPED)
    disco_mock = MagicMock()
    monkeypatch.setattr(mailSystem.mailDisco, "MailDisco", disco_mock)

    mailSystem.check_mail_job()

    assert "m1" in mailSystem.processed_ids
    save_run_mock.assert_called_once()
    # 書き込み失敗(❌)通知は送らない（欠損通知は sheet() 側で送信済み）
    assert not any("書き込みに失敗" in str(c.args[0]) for c in disco_mock.call_args_list)
