import logging
from unittest.mock import MagicMock, patch

import gspread

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
    def test_returns_false_immediately_when_gc_is_none(self, caplog):
        mailSystem.gc = None
        with patch("src.mailSystem.mailDisco.MailDisco") as mock_disco:
            with caplog.at_level(logging.ERROR, logger="smbc_card_recorder.src.mailSystem"):
                result = mailSystem.sheet("2026/11/05 12:00", "スーパーA", 1000)

        assert result is False
        mock_disco.assert_not_called()
        assert "GSpread認証が完了していません" in caplog.text

    def test_writes_to_first_empty_a_to_d_row_from_row_3(self):
        ws = MagicMock()
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
            result = mailSystem.sheet("2026/11/05 12:00", "スーパーA", 1200)

        assert result is True
        gc.open_by_key.assert_called_once_with("test-spreadsheet-id")
        spreadsheet.worksheet.assert_called_once_with("11月")
        mock_disco_cls.assert_called_once_with(
            "【正常に書き込みました】\nシート: 11月 (4行目)\n利用先: スーパーA\n金額: 1200"
        )
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
            [],
        ]
        gc, _ = _make_mock_gc({"5月": ws})
        mailSystem.gc = gc

        with patch("src.mailSystem.purpose.Purpose") as mock_purpose_cls, \
             patch("src.mailSystem.mailDisco.MailDisco"):
            mock_purpose_cls.return_value.judge.return_value = None
            assert mailSystem.sheet("2026/05/10 09:00", "未知の店", 500) is True

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
            assert mailSystem.sheet("2026/11/05 12:00", "JR東日本", 300) is True

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
            assert mailSystem.sheet("2026/11/05 12:00", "スーパーA", 400) is True

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
            assert mailSystem.sheet("2026/11/05 12:00", "Amazon", 999) is True

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
                assert mailSystem.sheet("2026/11/05 12:00", "スーパーA", 1000) is True

        ws.append_row.assert_called_once_with(["2026/11/05 12:00", "スーパーA", 1000, "食費"])
        assert "ワークシートの値の取得に失敗しました" in caplog.text

    def test_na_email_time_notifies_and_skips_write(self):
        ws = MagicMock()
        gc, spreadsheet = _make_mock_gc({"11月": ws})
        mailSystem.gc = gc

        with patch("src.mailSystem.purpose.Purpose") as mock_purpose_cls, \
             patch("src.mailSystem.mailDisco.MailDisco") as mock_disco_cls:
            result = mailSystem.sheet("N/A", "スーパーA", 1000)

        assert result == mailSystem.SHEET_SKIPPED
        spreadsheet.worksheet.assert_not_called()
        mock_purpose_cls.assert_not_called()
        mock_disco_cls.assert_called_once_with(
            "⚠️ 以下の項目を抽出できなかったため記録を中止しました: 利用日\n"
            "利用日: N/A / 利用先: スーパーA / 金額: 1000"
        )
        ws.append_row.assert_not_called()

    def test_na_location_notifies_and_skips_write(self):
        ws = MagicMock()
        gc, spreadsheet = _make_mock_gc({"Sheet1": ws})
        mailSystem.gc = gc

        with patch("src.mailSystem.purpose.Purpose") as mock_purpose_cls, \
             patch("src.mailSystem.mailDisco.MailDisco") as mock_disco_cls:
            result = mailSystem.sheet("2026/11/05 12:00", "N/A", 1000)

        assert result == mailSystem.SHEET_SKIPPED
        spreadsheet.worksheet.assert_not_called()
        mock_purpose_cls.assert_not_called()
        mock_disco_cls.assert_called_once_with(
            "⚠️ 以下の項目を抽出できなかったため記録を中止しました: 利用先\n"
            "利用日: 2026/11/05 12:00 / 利用先: N/A / 金額: 1000"
        )
        ws.append_row.assert_not_called()

    def test_na_money_notifies_and_skips_write(self):
        ws = MagicMock()
        gc, spreadsheet = _make_mock_gc({"Sheet1": ws})
        mailSystem.gc = gc

        with patch("src.mailSystem.purpose.Purpose") as mock_purpose_cls, \
             patch("src.mailSystem.mailDisco.MailDisco") as mock_disco_cls:
            result = mailSystem.sheet("2026/11/05 12:00", "レストランA", "N/A")

        assert result == mailSystem.SHEET_SKIPPED
        spreadsheet.worksheet.assert_not_called()
        mock_purpose_cls.assert_not_called()
        mock_disco_cls.assert_called_once_with(
            "⚠️ 以下の項目を抽出できなかったため記録を中止しました: 利用金額\n"
            "利用日: 2026/11/05 12:00 / 利用先: レストランA / 金額: N/A"
        )
        ws.append_row.assert_not_called()

    def test_na_consolidates_all_missing_fields(self):
        ws = MagicMock()
        gc, _ = _make_mock_gc({"11月": ws})
        mailSystem.gc = gc

        with patch("src.mailSystem.purpose.Purpose"), \
             patch("src.mailSystem.mailDisco.MailDisco") as mock_disco_cls:
            result = mailSystem.sheet("N/A", "N/A", "N/A")

        assert result == mailSystem.SHEET_SKIPPED
        mock_disco_cls.assert_called_once_with(
            "⚠️ 以下の項目を抽出できなかったため記録を中止しました: 利用日・利用先・利用金額\n"
            "利用日: N/A / 利用先: N/A / 金額: N/A"
        )

    def test_worksheet_not_found_falls_back_to_sheet1_and_notifies(self):
        ws_fallback = MagicMock()
        ws_fallback.get_all_values.return_value = []
        gc, spreadsheet = _make_mock_gc({"Sheet1": ws_fallback})
        mailSystem.gc = gc

        with patch("src.mailSystem.purpose.Purpose") as mock_purpose_cls, \
             patch("src.mailSystem.mailDisco.MailDisco") as mock_disco_cls:
            mock_purpose_cls.return_value.judge.return_value = "食費"
            assert mailSystem.sheet("2026/11/05 12:00", "スーパーA", 500) is True

        assert spreadsheet.worksheet.call_args_list[0].args == ("11月",)
        assert spreadsheet.worksheet.call_args_list[1].args == ("Sheet1",)
        messages = [c.args[0] for c in mock_disco_cls.call_args_list]
        assert any("ワークシート '11月' が見つからず、'Sheet1' (1行目) に書き込みました" in m for m in messages)
        ws_fallback.append_row.assert_called_once()

    def test_both_target_and_sheet1_not_found_aborts_write(self, caplog):
        gc, _ = _make_mock_gc({})
        mailSystem.gc = gc

        with patch("src.mailSystem.purpose.Purpose") as mock_purpose_cls, \
             patch("src.mailSystem.mailDisco.MailDisco") as mock_disco_cls:
            with caplog.at_level(logging.ERROR, logger="smbc_card_recorder.src.mailSystem"):
                assert mailSystem.sheet("2026/11/05 12:00", "スーパーA", 500) is False

        mock_purpose_cls.assert_not_called()
        mock_disco_cls.assert_called_once()
        assert "ワークシート 'Sheet1' も見つかりません" in caplog.text

    def test_spreadsheet_not_found_is_caught_logged_and_notified(self, caplog):
        gc = MagicMock()
        gc.open_by_key.side_effect = gspread.exceptions.SpreadsheetNotFound("not found")
        mailSystem.gc = gc

        with patch("src.mailSystem.mailDisco.MailDisco") as mock_disco_cls, \
             caplog.at_level(logging.ERROR, logger="smbc_card_recorder.src.mailSystem"):
            assert mailSystem.sheet("2026/11/05 12:00", "スーパーA", 500) is False

        mock_disco_cls.assert_called_once()
        assert "スプレッドシートが見つかりません" in caplog.text

    def test_unexpected_exception_during_update_is_caught_and_logged(self, caplog):
        ws = MagicMock()
        ws.get_all_values.return_value = [["h"], ["h"], [""]]
        ws.update.side_effect = RuntimeError("quota exceeded")
        gc, _ = _make_mock_gc({"11月": ws})
        mailSystem.gc = gc

        with patch("src.mailSystem.purpose.Purpose") as mock_purpose_cls, \
             patch("src.mailSystem.mailDisco.MailDisco") as mock_disco_cls:
            mock_purpose_cls.return_value.judge.return_value = "食費"
            with caplog.at_level(logging.ERROR, logger="smbc_card_recorder.src.mailSystem"):
                assert mailSystem.sheet("2026/11/05 12:00", "スーパーA", 500) is False

        mock_disco_cls.assert_called_once()
        assert "スプレッドシートへの書き込み中に予期せぬエラーが発生しました" in caplog.text
