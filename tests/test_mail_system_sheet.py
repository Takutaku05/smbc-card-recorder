import datetime
import logging
from unittest.mock import MagicMock, patch

import gspread
import pytest

import src.mailSystem as mailSystem


def _make_mock_gc(worksheet_map):
    """ワークシート名 -> MagicMock マップを返すモック gc を構築する。"""
    spreadsheet = MagicMock()

    def _get_worksheet(name):
        if name in worksheet_map:
            return worksheet_map[name]
        raise gspread.exceptions.WorksheetNotFound(name)

    spreadsheet.worksheet.side_effect = _get_worksheet
    gc = MagicMock()
    gc.open_by_key.return_value = spreadsheet
    return gc, spreadsheet


class TestSheetWriting:
    def test_returns_immediately_when_gc_is_none(self, caplog):
        mailSystem.gc = None
        with patch("src.mailSystem.mailDisco.MailDisco") as mock_disco:
            with caplog.at_level(logging.ERROR, logger="smbc_card_recorder.src.mailSystem"):
                mailSystem.sheet("2026/11/05 12:00", "スーパーA", 1000)

        mock_disco.assert_not_called()
        assert "GSpread認証が完了していません" in caplog.text

    def test_writes_to_first_empty_a_column_row_from_row_3(self):
        ws = MagicMock()
        # 1行目・2行目はA列が空でもスキップされる仕様。3行目は埋まっている。4行目が空き。
        ws.get_all_values.return_value = [
            ["", "ヘッダー1"],
            [],
            ["2026/11/01 10:00", "既存店", "500", "食費"],
            ["", "", "", ""],
            ["2026/11/02 10:00", "別の店", "800", "外食費"],
        ]
        gc, spreadsheet = _make_mock_gc({"11月": ws})
        mailSystem.gc = gc

        with patch("src.mailSystem.purpose.Purpose") as mock_purpose_cls, \
             patch("src.mailSystem.mailDisco.MailDisco") as mock_disco_cls:
            mock_purpose_cls.return_value.judge.return_value = "食費"
            mailSystem.sheet("2026/11/05 12:00", "スーパーA", 1200)

        gc.open_by_key.assert_called_once_with("test-spreadsheet-id")
        spreadsheet.worksheet.assert_called_once_with("11月")
        mock_disco_cls.assert_called_once_with("【正常に書き込みました】\n利用先: スーパーA")
        mock_disco_cls.return_value.send.assert_called_once()
        ws.update.assert_called_once_with(
            values=[["2026/11/05 12:00", "スーパーA", 1200, "食費"]],
            range_name="A4:D4",
        )
        ws.append_row.assert_not_called()

    def test_writes_to_row_3_when_row_3_is_empty_list(self):
        ws = MagicMock()
        ws.get_all_values.return_value = [
            ["ヘッダー"],
            ["サブヘッダー"],
            [],  # index 2 -> 3行目が空リスト
        ]
        gc, _ = _make_mock_gc({"5月": ws})
        mailSystem.gc = gc

        with patch("src.mailSystem.purpose.Purpose") as mock_purpose_cls, \
             patch("src.mailSystem.mailDisco.MailDisco"):
            mock_purpose_cls.return_value.judge.return_value = None
            mailSystem.sheet("2026/05/10 09:00", "未知の店", 500)

        ws.update.assert_called_once_with(
            values=[["2026/05/10 09:00", "未知の店", 500, None]],
            range_name="A3:D3",
        )

    def test_appends_row_when_no_empty_row_found_from_row_3(self):
        ws = MagicMock()
        ws.get_all_values.return_value = [
            ["", "タイトル"],
            ["", "サブ"],
            ["2026/11/01 10:00", "店1", "100", "食費"],
            ["2026/11/02 10:00", "店2", "200", "食費"],
        ]
        gc, _ = _make_mock_gc({"11月": ws})
        mailSystem.gc = gc

        with patch("src.mailSystem.purpose.Purpose") as mock_purpose_cls, \
             patch("src.mailSystem.mailDisco.MailDisco"):
            mock_purpose_cls.return_value.judge.return_value = "交通費"
            mailSystem.sheet("2026/11/05 12:00", "JR東日本", 300)

        ws.append_row.assert_called_once_with(["2026/11/05 12:00", "JR東日本", 300, "交通費"])
        ws.update.assert_not_called()

    def test_appends_row_when_only_header_rows_exist(self):
        ws = MagicMock()
        ws.get_all_values.return_value = [
            ["日時", "場所", "金額", "用途"],
            ["", "", "", ""],
        ]
        gc, _ = _make_mock_gc({"11月": ws})
        mailSystem.gc = gc

        with patch("src.mailSystem.purpose.Purpose") as mock_purpose_cls, \
             patch("src.mailSystem.mailDisco.MailDisco"):
            mock_purpose_cls.return_value.judge.return_value = "食費"
            mailSystem.sheet("2026/11/05 12:00", "スーパーA", 400)

        ws.append_row.assert_called_once_with(["2026/11/05 12:00", "スーパーA", 400, "食費"])
        ws.update.assert_not_called()

    def test_appends_row_when_worksheet_is_completely_empty(self):
        ws = MagicMock()
        ws.get_all_values.return_value = []
        gc, _ = _make_mock_gc({"11月": ws})
        mailSystem.gc = gc

        with patch("src.mailSystem.purpose.Purpose") as mock_purpose_cls, \
             patch("src.mailSystem.mailDisco.MailDisco"):
            mock_purpose_cls.return_value.judge.return_value = "その他"
            mailSystem.sheet("2026/11/05 12:00", "Amazon", 999)

        ws.append_row.assert_called_once_with(["2026/11/05 12:00", "Amazon", 999, "その他"])
        ws.update.assert_not_called()

    def test_appends_row_when_get_all_values_raises_exception(self, caplog):
        ws = MagicMock()
        ws.get_all_values.side_effect = RuntimeError("API temporary error")
        gc, _ = _make_mock_gc({"11月": ws})
        mailSystem.gc = gc

        with patch("src.mailSystem.purpose.Purpose") as mock_purpose_cls, \
             patch("src.mailSystem.mailDisco.MailDisco"):
            mock_purpose_cls.return_value.judge.return_value = "食費"
            with caplog.at_level(logging.WARNING, logger="smbc_card_recorder.src.mailSystem"):
                mailSystem.sheet("2026/11/05 12:00", "スーパーA", 1000)

        ws.append_row.assert_called_once_with(["2026/11/05 12:00", "スーパーA", 1000, "食費"])
        assert "ワークシートの値の取得に失敗しました" in caplog.text

    def test_na_email_time_notifies_and_uses_current_month_sheet(self):
        current_month_sheet = f"{datetime.datetime.now().month}月"
        ws = MagicMock()
        ws.get_all_values.return_value = []
        gc, spreadsheet = _make_mock_gc({current_month_sheet: ws})
        mailSystem.gc = gc

        with patch("src.mailSystem.purpose.Purpose") as mock_purpose_cls, \
             patch("src.mailSystem.mailDisco.MailDisco") as mock_disco_cls:
            mock_purpose_cls.return_value.judge.return_value = "食費"
            mailSystem.sheet("N/A", "スーパーA", 1000)

        spreadsheet.worksheet.assert_called_once_with(current_month_sheet)
        mock_disco_cls.assert_called_once_with("⚠️ 利用日が抽出できませんでした。")
        ws.append_row.assert_called_once_with(["N/A", "スーパーA", 1000, "食費"])

    def test_na_location_notifies_and_uses_sheet1(self):
        ws = MagicMock()
        ws.get_all_values.return_value = []
        gc, spreadsheet = _make_mock_gc({"Sheet1": ws})
        mailSystem.gc = gc

        with patch("src.mailSystem.purpose.Purpose") as mock_purpose_cls, \
             patch("src.mailSystem.mailDisco.MailDisco") as mock_disco_cls:
            mock_purpose_cls.return_value.judge.return_value = None
            mailSystem.sheet("2026/11/05 12:00", "N/A", 1000)

        spreadsheet.worksheet.assert_called_once_with("Sheet1")
        mock_disco_cls.assert_called_once_with("⚠️ 利用先が抽出できませんでした。")
        ws.append_row.assert_called_once_with(["2026/11/05 12:00", "N/A", 1000, None])

    def test_na_money_notifies_and_uses_sheet1(self):
        ws = MagicMock()
        ws.get_all_values.return_value = []
        gc, spreadsheet = _make_mock_gc({"Sheet1": ws})
        mailSystem.gc = gc

        with patch("src.mailSystem.purpose.Purpose") as mock_purpose_cls, \
             patch("src.mailSystem.mailDisco.MailDisco") as mock_disco_cls:
            mock_purpose_cls.return_value.judge.return_value = "外食費"
            mailSystem.sheet("2026/11/05 12:00", "レストランA", "N/A")

        spreadsheet.worksheet.assert_called_once_with("Sheet1")
        mock_disco_cls.assert_called_once_with("⚠️ 利用金額が抽出できませんでした。")
        ws.append_row.assert_called_once_with(["2026/11/05 12:00", "レストランA", "N/A", "外食費"])

    def test_na_priority_when_multiple_fields_missing(self):
        """現行mainの仕様: elifチェーンにより email_time -> location -> money の順で判定される。"""
        current_month_sheet = f"{datetime.datetime.now().month}月"
        ws = MagicMock()
        ws.get_all_values.return_value = []
        gc, _ = _make_mock_gc({current_month_sheet: ws})
        mailSystem.gc = gc

        with patch("src.mailSystem.purpose.Purpose") as mock_purpose_cls, \
             patch("src.mailSystem.mailDisco.MailDisco") as mock_disco_cls:
            mock_purpose_cls.return_value.judge.return_value = None
            mailSystem.sheet("N/A", "N/A", "N/A")

        mock_disco_cls.assert_called_once_with("⚠️ 利用日が抽出できませんでした。")

    def test_worksheet_not_found_falls_back_to_sheet1_and_notifies(self):
        ws_fallback = MagicMock()
        ws_fallback.get_all_values.return_value = []
        gc, spreadsheet = _make_mock_gc({"Sheet1": ws_fallback})
        mailSystem.gc = gc

        with patch("src.mailSystem.purpose.Purpose") as mock_purpose_cls, \
             patch("src.mailSystem.mailDisco.MailDisco") as mock_disco_cls:
            mock_purpose_cls.return_value.judge.return_value = "食費"
            mailSystem.sheet("2026/11/05 12:00", "スーパーA", 500)

        assert spreadsheet.worksheet.call_args_list[0].args == ("11月",)
        assert spreadsheet.worksheet.call_args_list[1].args == ("Sheet1",)
        messages = [c.args[0] for c in mock_disco_cls.call_args_list]
        assert any("正常に書き込みました" in m for m in messages)
        assert any("ワークシート '11月' が見つからず、 'Sheet1' に書き込みました" in m for m in messages)
        ws_fallback.append_row.assert_called_once()

    def test_both_target_and_sheet1_not_found_aborts_write(self, caplog):
        gc, _ = _make_mock_gc({})
        mailSystem.gc = gc

        with patch("src.mailSystem.purpose.Purpose") as mock_purpose_cls, \
             patch("src.mailSystem.mailDisco.MailDisco"):
            with caplog.at_level(logging.ERROR, logger="smbc_card_recorder.src.mailSystem"):
                mailSystem.sheet("2026/11/05 12:00", "スーパーA", 500)

        mock_purpose_cls.assert_not_called()
        assert "ワークシート 'Sheet1' も見つかりません" in caplog.text

    def test_spreadsheet_not_found_is_caught_and_logged(self, caplog):
        gc = MagicMock()
        gc.open_by_key.side_effect = gspread.exceptions.SpreadsheetNotFound("not found")
        mailSystem.gc = gc

        with caplog.at_level(logging.ERROR, logger="smbc_card_recorder.src.mailSystem"):
            mailSystem.sheet("2026/11/05 12:00", "スーパーA", 500)

        assert "スプレッドシートが見つかりません" in caplog.text

    def test_unexpected_exception_during_update_is_caught_and_logged(self, caplog):
        ws = MagicMock()
        ws.get_all_values.return_value = [["h"], ["h"], [""]]
        ws.update.side_effect = RuntimeError("quota exceeded")
        gc, _ = _make_mock_gc({"11月": ws})
        mailSystem.gc = gc

        with patch("src.mailSystem.purpose.Purpose") as mock_purpose_cls, \
             patch("src.mailSystem.mailDisco.MailDisco"):
            mock_purpose_cls.return_value.judge.return_value = "食費"
            with caplog.at_level(logging.ERROR, logger="smbc_card_recorder.src.mailSystem"):
                mailSystem.sheet("2026/11/05 12:00", "スーパーA", 500)

        assert "スプレッドシートへの書き込み中に予期せぬエラーが発生しました" in caplog.text
