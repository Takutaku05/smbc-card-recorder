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


def _run_sheet_with_rows(monkeypatch, all_values):
    worksheet = MagicMock()
    worksheet.get_all_values.return_value = all_values
    spreadsheet = MagicMock()
    spreadsheet.worksheet.return_value = worksheet
    gc = MagicMock()
    gc.open_by_key.return_value = spreadsheet

    monkeypatch.setattr(mailSystem, "gc", gc)
    monkeypatch.setattr(mailSystem.purpose, "Purpose", _DummyPurpose)
    monkeypatch.setattr(mailSystem.mailDisco, "MailDisco", MagicMock())

    assert mailSystem.sheet("2026/01/05 12:00", "テスト店", 1200) is True
    return worksheet


def test_does_not_overwrite_total_row_with_empty_col_a(monkeypatch):
    worksheet = _run_sheet_with_rows(
        monkeypatch,
        [
            ["利用日時", "利用先", "金額", "用途"],
            ["", "", "", ""],
            ["", "合計", "=SUM(C3:C50)", ""],
            ["", "", "", ""],
        ],
    )
    worksheet.update.assert_called_once_with(
        values=[["2026/01/05 12:00", "テスト店", 1200, "食費"]],
        range_name="A4:D4",
    )
    worksheet.append_row.assert_not_called()


def test_does_not_overwrite_manual_entry_row_with_empty_col_a(monkeypatch):
    worksheet = _run_sheet_with_rows(
        monkeypatch,
        [
            ["利用日時", "利用先", "金額", "用途"],
            ["", "", "", ""],
            ["", "現金支出", "800", "食費"],
        ],
    )
    worksheet.update.assert_not_called()
    worksheet.append_row.assert_called_once_with(["2026/01/05 12:00", "テスト店", 1200, "食費"])


def test_whitespace_only_cells_in_a_to_d_are_treated_as_empty(monkeypatch):
    worksheet = _run_sheet_with_rows(
        monkeypatch,
        [
            ["H1", "H2", "H3", "H4"],
            ["", "", "", ""],
            [" ", "  ", "", "\t"],
        ],
    )
    worksheet.update.assert_called_once_with(
        values=[["2026/01/05 12:00", "テスト店", 1200, "食費"]],
        range_name="A3:D3",
    )


def test_row_with_memo_only_in_col_e_allows_writing_a_to_d(monkeypatch):
    worksheet = _run_sheet_with_rows(
        monkeypatch,
        [
            ["H1", "H2", "H3", "H4", "メモ"],
            ["", "", "", "", ""],
            ["", "", "", "", "欄外メモ"],
        ],
    )
    worksheet.update.assert_called_once_with(
        values=[["2026/01/05 12:00", "テスト店", 1200, "食費"]],
        range_name="A3:D3",
    )


def test_short_row_in_data_area_is_treated_as_empty(monkeypatch):
    worksheet = _run_sheet_with_rows(
        monkeypatch,
        [
            ["H1"],
            ["H2"],
            [],
        ],
    )
    worksheet.update.assert_called_once_with(
        values=[["2026/01/05 12:00", "テスト店", 1200, "食費"]],
        range_name="A3:D3",
    )
