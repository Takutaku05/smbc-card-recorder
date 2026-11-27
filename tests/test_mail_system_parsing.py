import datetime
import logging
from unittest.mock import patch

import pytest

import src.mailSystem as mailSystem


class TestTimesheet:
    @pytest.mark.parametrize(
        "time_str,expected",
        [
            ("2026/01/05 09:00", "1月"),
            ("2026/02/28 23:59", "2月"),
            ("2026/03/15 12:30:45", "3月"),
            ("2026/04/01 00:00:00", "4月"),
            ("2026/09/30 18:15", "9月"),
            ("2026/10/10 10:10:10", "10月"),
            ("2026/11/05 12:00", "11月"),
            ("2026/12/31 23:59:59", "12月"),
        ],
    )
    def test_timesheet_parses_both_hhmm_and_hhmmss_formats(self, time_str, expected):
        assert mailSystem.timesheet(time_str) == expected

    @pytest.mark.parametrize(
        "invalid_time",
        [
            "N/A",
            "",
            "2026-11-05 12:00",
            "2026/11/05",
            "2026/02/30 12:00",
            "2026/13/01 12:00",
            "invalid",
        ],
    )
    def test_timesheet_invalid_format_falls_back_to_current_month(self, invalid_time, caplog):
        expected_month = f"{datetime.datetime.now().month}月"

        with caplog.at_level(logging.WARNING, logger="smbc_card_recorder.src.mailSystem"):
            result = mailSystem.timesheet(invalid_time)

        assert result == expected_month
        assert "形式が不正です" in caplog.text


class TestSystemParsing:
    def test_system_extracts_credit_card_format(self):
        body = (
            "三井住友カードをご利用いただきありがとうございます。\n"
            "ご利用日時：2026/11/27 14:35\n"
            "ご利用場所：スーパーA\n"
            "ご利用金額：3,480円\n"
            "今後ともよろしくお願いいたします。"
        )
        with patch.object(mailSystem, "sheet") as mock_sheet:
            mailSystem.system(body)

        mock_sheet.assert_called_once_with("2026/11/27 14:35", "スーパーA", 3480)

    def test_system_extracts_debit_card_format_with_seconds_and_halfwidth_colon(self):
        body = (
            "利用日: 2026/07/03 08:15:42\n"
            "利用先:   JR東日本 渋谷駅  \n"
            "利用金額: 210円\n"
        )
        with patch.object(mailSystem, "sheet") as mock_sheet:
            mailSystem.system(body)

        mock_sheet.assert_called_once_with("2026/07/03 08:15:42", "JR東日本 渋谷駅", 210)

    def test_system_extracts_amount_without_colon_and_large_commas(self):
        body = (
            "ご利用日時:2026/12/01 20:00\n"
            "ご利用場所:家電量販店\n"
            "ご利用金額 1,234,567円\n"
        )
        with patch.object(mailSystem, "sheet") as mock_sheet:
            mailSystem.system(body)

        mock_sheet.assert_called_once_with("2026/12/01 20:00", "家電量販店", 1234567)

    def test_system_extracts_zero_and_negative_amount(self):
        body_zero = "利用日: 2026/05/01 10:00\n利用先: テスト店\n利用金額: 0円"
        body_neg = "利用日: 2026/05/01 10:00\n利用先: 返金店舗\n利用金額: -1,500円"

        with patch.object(mailSystem, "sheet") as mock_sheet:
            mailSystem.system(body_zero)
            mailSystem.system(body_neg)

        assert mock_sheet.call_args_list[0].args == ("2026/05/01 10:00", "テスト店", 0)
        assert mock_sheet.call_args_list[1].args == ("2026/05/01 10:00", "返金店舗", -1500)

    def test_system_keeps_raw_string_when_amount_int_conversion_fails(self, caplog):
        """正規表現 [-?\\d,]+ にマッチするが int() に失敗する値（例: --100）は文字列のまま渡される。"""
        body = (
            "利用日: 2026/05/01 10:00\n"
            "利用先: テスト店\n"
            "利用金額: --100円\n"
        )
        with patch.object(mailSystem, "sheet") as mock_sheet:
            with caplog.at_level(logging.WARNING, logger="smbc_card_recorder.src.mailSystem"):
                mailSystem.system(body)

        mock_sheet.assert_called_once_with("2026/05/01 10:00", "テスト店", "--100")
        assert "数値に変換できませんでした" in caplog.text

    def test_system_missing_datetime_passes_na(self):
        body = "利用先: コンビニC\n利用金額: 500円"
        with patch.object(mailSystem, "sheet") as mock_sheet:
            mailSystem.system(body)

        mock_sheet.assert_called_once_with("N/A", "コンビニC", 500)

    def test_system_missing_location_passes_na(self):
        body = "利用日: 2026/05/01 10:00\n利用金額: 500円"
        with patch.object(mailSystem, "sheet") as mock_sheet:
            mailSystem.system(body)

        mock_sheet.assert_called_once_with("2026/05/01 10:00", "N/A", 500)

    def test_system_missing_amount_passes_na(self):
        body = "利用日: 2026/05/01 10:00\n利用先: コンビニC"
        with patch.object(mailSystem, "sheet") as mock_sheet:
            mailSystem.system(body)

        mock_sheet.assert_called_once_with("2026/05/01 10:00", "コンビニC", "N/A")

    def test_system_completely_unmatched_body_passes_all_na(self):
        with patch.object(mailSystem, "sheet") as mock_sheet:
            mailSystem.system("無関係なメール本文です。")

        mock_sheet.assert_called_once_with("N/A", "N/A", "N/A")
