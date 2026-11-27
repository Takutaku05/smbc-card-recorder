"""sheet() / timesheet() の追加回帰テスト（空き行探索の境界・通知内容・ログ・呼び出し引数）。"""

import logging
from unittest.mock import MagicMock, call, patch

import gspread
import pytest

import src.mailSystem as mailSystem

LOGGER = "smbc_card_recorder.src.mailSystem"


def _gc_with(worksheets):
    spreadsheet = MagicMock()

    def _ws(name):
        if name in worksheets:
            return worksheets[name]
        raise gspread.exceptions.WorksheetNotFound(name)

    spreadsheet.worksheet.side_effect = _ws
    gc = MagicMock()
    gc.open_by_key.return_value = spreadsheet
    return gc, spreadsheet


def _ws(values):
    ws = MagicMock()
    ws.get_all_values.return_value = values
    return ws


@pytest.fixture
def purpose_food():
    with patch("src.mailSystem.purpose.Purpose") as cls:
        cls.return_value.judge.return_value = "食費"
        yield cls


@pytest.fixture
def disco():
    with patch("src.mailSystem.mailDisco.MailDisco") as cls:
        yield cls


HEADER = [["日時", "場所", "金額", "用途"], ["", "", "", ""]]


class TestEmptyRowSearch:
    @pytest.mark.parametrize(
        "rows_after_header,expected_range",
        [
            ([[""]], "A3:D3"),
            ([[]], "A3:D3"),
            ([["x"], [""]], "A4:D4"),
            ([["x"], ["y"], [], [""]], "A5:D5"),  # 最初の空き行が選ばれる
            ([["x"], ["", "", "", ""], [""]], "A4:D4"),
        ],
    )
    def test_first_empty_a_column_row_from_row3(
        self, purpose_food, disco, rows_after_header, expected_range
    ):
        ws = _ws(HEADER + rows_after_header)
        mailSystem.gc, _ = _gc_with({"11月": ws})
        mailSystem.sheet("2026/11/05 12:00", "店", 100)
        ws.update.assert_called_once_with(
            values=[["2026/11/05 12:00", "店", 100, "食費"]], range_name=expected_range
        )
        ws.append_row.assert_not_called()

    @pytest.mark.known_issue(15)
    def test_only_column_a_is_checked_other_columns_may_have_content(
        self, purpose_food, disco
    ):
        """現行仕様: A列だけで空き判定する（B〜D列に値があっても空き行扱い）。Issue #15 参照。"""
        ws = _ws(HEADER + [["", "メモ", "", "=SUM(C:C)"]])
        mailSystem.gc, _ = _gc_with({"11月": ws})
        mailSystem.sheet("2026/11/05 12:00", "店", 100)
        ws.update.assert_called_once_with(
            values=[["2026/11/05 12:00", "店", 100, "食費"]], range_name="A3:D3"
        )

    def test_whitespace_in_column_a_is_not_treated_as_empty(self, purpose_food, disco):
        ws = _ws(HEADER + [[" "], ["　"]])
        mailSystem.gc, _ = _gc_with({"11月": ws})
        mailSystem.sheet("2026/11/05 12:00", "店", 100)
        ws.update.assert_not_called()
        ws.append_row.assert_called_once_with(["2026/11/05 12:00", "店", 100, "食費"])

    def test_single_header_row_appends(self, purpose_food, disco):
        ws = _ws([["日時"]])
        mailSystem.gc, _ = _gc_with({"11月": ws})
        mailSystem.sheet("2026/11/05 12:00", "店", 100)
        ws.append_row.assert_called_once()
        ws.update.assert_not_called()

    def test_large_sheet_row_number(self, purpose_food, disco):
        ws = _ws(HEADER + [["x"]] * 997 + [[""]])
        mailSystem.gc, _ = _gc_with({"11月": ws})
        mailSystem.sheet("2026/11/05 12:00", "店", 100)
        assert ws.update.call_args.kwargs["range_name"] == "A1000:D1000"

    def test_get_all_values_failure_never_calls_update(self, purpose_food, disco):
        ws = MagicMock()
        ws.get_all_values.side_effect = gspread.exceptions.APIError(
            MagicMock(json=lambda: {"error": {"code": 500, "message": "x"}})
        )
        mailSystem.gc, _ = _gc_with({"11月": ws})
        mailSystem.sheet("2026/11/05 12:00", "店", 100)
        ws.update.assert_not_called()
        ws.append_row.assert_called_once_with(["2026/11/05 12:00", "店", 100, "食費"])

    @pytest.mark.parametrize(
        "values,expected_log",
        [
            ([], "シートが空だったため、1行目にデータ"),
            (HEADER + [["x"]], "A列が空の行が見つからなかったため、最終行にデータ"),
            (HEADER + [[""]], "3行目 (A3:D3) にデータ"),
        ],
    )
    def test_write_location_is_logged(
        self, purpose_food, disco, caplog, values, expected_log
    ):
        mailSystem.gc, _ = _gc_with({"11月": _ws(values)})
        with caplog.at_level(logging.INFO, logger=LOGGER):
            mailSystem.sheet("2026/11/05 12:00", "店", 100)
        assert expected_log in caplog.text
        assert "['2026/11/05 12:00', '店', 100, '食費']" in caplog.text


class TestSheetInputsAndCalls:
    def test_purpose_receives_location_and_judge_result_goes_to_column_d(self, disco):
        ws = _ws([])
        mailSystem.gc, _ = _gc_with({"11月": ws})
        with patch("src.mailSystem.purpose.Purpose") as cls:
            cls.return_value.judge.return_value = "交通費"
            mailSystem.sheet("2026/11/05 12:00", "JR東日本", 210)
        cls.assert_called_once_with("JR東日本")
        cls.return_value.judge.assert_called_once_with()
        ws.append_row.assert_called_once_with(
            ["2026/11/05 12:00", "JR東日本", 210, "交通費"]
        )

    def test_spreadsheet_opened_by_configured_id(
        self, purpose_food, disco, monkeypatch
    ):
        monkeypatch.setattr(mailSystem, "SPREADSHEET_ID", "abc123")
        mailSystem.gc, _ = _gc_with({"11月": _ws([])})
        mailSystem.sheet("2026/11/05 12:00", "店", 100)
        mailSystem.gc.open_by_key.assert_called_once_with("abc123")

    @pytest.mark.parametrize(
        "email_time,sheet_name",
        [
            ("2026/01/05 12:00", "1月"),  # ゼロ埋めしない
            ("2026/12/31 23:59:59", "12月"),
            ("2027/03/01 00:00", "3月"),
        ],
    )
    def test_target_sheet_name_from_email_time(
        self, purpose_food, disco, email_time, sheet_name
    ):
        mailSystem.gc, spreadsheet = _gc_with({sheet_name: _ws([])})
        mailSystem.sheet(email_time, "店", 1)
        spreadsheet.worksheet.assert_called_once_with(sheet_name)

    def test_amount_written_as_is_including_string(self, purpose_food, disco):
        ws = _ws([])
        mailSystem.gc, _ = _gc_with({"11月": ws})
        mailSystem.sheet("2026/11/05 12:00", "店", "--100")
        ws.append_row.assert_called_once_with(
            ["2026/11/05 12:00", "店", "--100", "食費"]
        )

    def test_gc_none_does_not_log_unexpected_error(self, caplog):
        mailSystem.gc = None
        with caplog.at_level(logging.ERROR, logger=LOGGER):
            mailSystem.sheet("2026/11/05 12:00", "店", 100)
        messages = [r.getMessage() for r in caplog.records]
        assert messages == ["GSpread認証が完了していません。データを書き込めません。"]


class TestSheetNotificationsAndFallback:
    def test_success_notification_exact_text_and_single_send(self, purpose_food, disco):
        mailSystem.gc, _ = _gc_with({"11月": _ws([])})
        mailSystem.sheet("2026/11/05 12:00", "スーパーA", 100)
        assert disco.call_args_list == [
            call("【正常に書き込みました】\n利用先: スーパーA")
        ]
        assert disco.return_value.send.call_count == 1

    def test_fallback_notifications_order(self, purpose_food, disco, caplog):
        ws1 = _ws([])
        mailSystem.gc, spreadsheet = _gc_with({"Sheet1": ws1})
        with caplog.at_level(logging.INFO, logger=LOGGER):
            mailSystem.sheet("2026/07/05 12:00", "店", 100)
        assert disco.call_args_list == [
            call("【正常に書き込みました】\n利用先: 店"),
            call("⚠️ ワークシート '7月' が見つからず、 'Sheet1' に書き込みました。"),
        ]
        assert disco.return_value.send.call_count == 2
        assert spreadsheet.worksheet.call_args_list == [call("7月"), call("Sheet1")]
        assert "ワークシート '7月' が見つかりません。" in caplog.text
        assert "フォールバックとして 'Sheet1' への書き込みを試みます。" in caplog.text
        ws1.append_row.assert_called_once_with(["2026/07/05 12:00", "店", 100, "食費"])

    def test_missing_location_with_missing_sheet1_aborts(self, purpose_food, disco):
        """N/A 時の書き込み先 Sheet1 が無い場合、フォールバックも Sheet1 なので中止される。"""
        mailSystem.gc, spreadsheet = _gc_with({})
        mailSystem.sheet("2026/07/05 12:00", "N/A", 100)
        assert spreadsheet.worksheet.call_args_list == [call("Sheet1"), call("Sheet1")]
        purpose_food.assert_not_called()

    def test_unexpected_error_is_logged_with_traceback(
        self, purpose_food, disco, caplog
    ):
        ws = _ws([])
        ws.append_row.side_effect = RuntimeError("quota")
        mailSystem.gc, _ = _gc_with({"11月": ws})
        with caplog.at_level(logging.ERROR, logger=LOGGER):
            mailSystem.sheet("2026/11/05 12:00", "店", 100)  # 例外は外に出ない
        records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert len(records) == 1
        assert records[0].exc_info and records[0].exc_info[0] is not None
        assert "quota" in records[0].getMessage()

    def test_spreadsheet_not_found_log_contains_id(self, caplog, monkeypatch):
        monkeypatch.setattr(mailSystem, "SPREADSHEET_ID", "missing-id")
        gc = MagicMock()
        gc.open_by_key.side_effect = gspread.exceptions.SpreadsheetNotFound
        mailSystem.gc = gc
        with (
            patch("src.mailSystem.mailDisco.MailDisco") as disco,
            caplog.at_level(logging.ERROR, logger=LOGGER),
        ):
            mailSystem.sheet("2026/11/05 12:00", "店", 100)
        assert "IDが正しいか確認してください: missing-id" in caplog.text
        disco.assert_not_called()

    def test_errors_from_sheet_do_not_propagate_to_caller(self, purpose_food, disco):
        gc = MagicMock()
        gc.open_by_key.side_effect = Exception("anything")
        mailSystem.gc = gc
        mailSystem.sheet("2026/11/05 12:00", "店", 100)  # 例外が出なければOK
