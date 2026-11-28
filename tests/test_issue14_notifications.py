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

import gspread as _gspread

from src import mailSystem


class _DummyPurpose:
    def __init__(self, location):
        self.location = location

    def judge(self):
        return "食費"


def _setup_discord_recorder(monkeypatch, events):
    messages = []

    class _Recorder:
        def __init__(self, message):
            self.message = message

        def send(self):
            events.append(("discord", self.message))
            messages.append(self.message)

    monkeypatch.setattr(mailSystem.mailDisco, "MailDisco", _Recorder)
    return messages


def test_update_success_sends_notification_after_write(monkeypatch):
    events = []
    messages = _setup_discord_recorder(monkeypatch, events)

    worksheet = MagicMock()
    worksheet.get_all_values.return_value = [["H1"], ["H2"], ["", "", "", ""]]

    def _update(**kwargs):
        events.append(("update", kwargs["range_name"]))

    worksheet.update.side_effect = _update
    spreadsheet = MagicMock()
    spreadsheet.worksheet.return_value = worksheet
    gc = MagicMock()
    gc.open_by_key.return_value = spreadsheet

    monkeypatch.setattr(mailSystem, "gc", gc)
    monkeypatch.setattr(mailSystem.purpose, "Purpose", _DummyPurpose)

    assert mailSystem.sheet("2026/01/05 12:00", "テスト店", 1500) is True
    assert events == [
        ("update", "A3:D3"),
        ("discord", "【正常に書き込みました】\nシート: 1月 (3行目)\n利用先: テスト店\n金額: 1500"),
    ]
    assert len(messages) == 1


def test_append_success_sends_notification_after_write(monkeypatch):
    events = []
    _setup_discord_recorder(monkeypatch, events)

    worksheet = MagicMock()
    worksheet.get_all_values.return_value = [["H1"], ["H2"], ["2026/01/01 09:00", "既存", 200, "食費"]]
    worksheet.append_row.side_effect = lambda _row: events.append(("append_row",))

    spreadsheet = MagicMock()
    spreadsheet.worksheet.return_value = worksheet
    gc = MagicMock()
    gc.open_by_key.return_value = spreadsheet

    monkeypatch.setattr(mailSystem, "gc", gc)
    monkeypatch.setattr(mailSystem.purpose, "Purpose", _DummyPurpose)

    assert mailSystem.sheet("2026/01/05 12:00", "テスト店", 1500) is True
    assert events[0] == ("append_row",)
    assert events[1][0] == "discord"
    assert "【正常に書き込みました】" in events[1][1]
    assert "最終行" in events[1][1]


def test_update_failure_never_sends_success_notification(monkeypatch):
    events = []
    messages = _setup_discord_recorder(monkeypatch, events)

    worksheet = MagicMock()
    worksheet.get_all_values.return_value = [["H1"], ["H2"], ["", "", "", ""]]
    worksheet.update.side_effect = RuntimeError("API 429")

    spreadsheet = MagicMock()
    spreadsheet.worksheet.return_value = worksheet
    gc = MagicMock()
    gc.open_by_key.return_value = spreadsheet

    monkeypatch.setattr(mailSystem, "gc", gc)
    monkeypatch.setattr(mailSystem.purpose, "Purpose", _DummyPurpose)

    assert mailSystem.sheet("2026/01/05 12:00", "テスト店", 1500) is False
    assert len(messages) == 1
    assert "正常に書き込みました" not in messages[0]
    assert messages[0].startswith("❌")


def test_missing_target_and_sheet1_sends_failure_notification(monkeypatch):
    events = []
    messages = _setup_discord_recorder(monkeypatch, events)

    spreadsheet = MagicMock()
    spreadsheet.worksheet.side_effect = _gspread.exceptions.WorksheetNotFound("not found")
    gc = MagicMock()
    gc.open_by_key.return_value = spreadsheet

    monkeypatch.setattr(mailSystem, "gc", gc)

    assert mailSystem.sheet("2026/01/05 12:00", "テスト店", 1500) is False
    assert len(messages) == 1
    assert "正常に書き込みました" not in messages[0]
    assert messages[0].startswith("❌")


def test_fallback_to_sheet1_notifies_only_after_write(monkeypatch):
    events = []
    messages = _setup_discord_recorder(monkeypatch, events)

    sheet1 = MagicMock()
    sheet1.get_all_values.return_value = []
    sheet1.append_row.side_effect = lambda _row: events.append(("append_row",))

    def _get_worksheet(name):
        if name == "Sheet1":
            return sheet1
        raise _gspread.exceptions.WorksheetNotFound(name)

    spreadsheet = MagicMock()
    spreadsheet.worksheet.side_effect = _get_worksheet
    gc = MagicMock()
    gc.open_by_key.return_value = spreadsheet

    monkeypatch.setattr(mailSystem, "gc", gc)
    monkeypatch.setattr(mailSystem.purpose, "Purpose", _DummyPurpose)

    assert mailSystem.sheet("2026/01/05 12:00", "テスト店", 1500) is True
    assert events[0] == ("append_row",)
    assert len(messages) == 1
    assert "Sheet1" in messages[0]
    assert "正常に書き込みました" not in messages[0]


def test_fallback_to_sheet1_write_error_sends_failure_not_fallback_success(monkeypatch):
    events = []
    messages = _setup_discord_recorder(monkeypatch, events)

    sheet1 = MagicMock()
    sheet1.get_all_values.return_value = []
    sheet1.append_row.side_effect = RuntimeError("write error")

    def _get_worksheet(name):
        if name == "Sheet1":
            return sheet1
        raise _gspread.exceptions.WorksheetNotFound(name)

    spreadsheet = MagicMock()
    spreadsheet.worksheet.side_effect = _get_worksheet
    gc = MagicMock()
    gc.open_by_key.return_value = spreadsheet

    monkeypatch.setattr(mailSystem, "gc", gc)
    monkeypatch.setattr(mailSystem.purpose, "Purpose", _DummyPurpose)

    assert mailSystem.sheet("2026/01/05 12:00", "テスト店", 1500) is False
    assert len(messages) == 1
    assert messages[0].startswith("❌")


def test_spreadsheet_not_found_sends_failure_notification(monkeypatch):
    events = []
    messages = _setup_discord_recorder(monkeypatch, events)

    gc = MagicMock()
    gc.open_by_key.side_effect = _gspread.exceptions.SpreadsheetNotFound("missing")
    monkeypatch.setattr(mailSystem, "gc", gc)

    assert mailSystem.sheet("2026/01/05 12:00", "テスト店", 1500) is False
    assert len(messages) == 1
    assert messages[0].startswith("❌")
