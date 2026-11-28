import sys
import types

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


def test_parse_mail_body_is_pure_and_extracts_expected_dict():
    body = (
        "いつも三井住友カードをご利用いただきありがとうございます。\n"
        "ご利用日時：2026/09/06 12:34:56\n"
        "ご利用先：セブンイレブン 渋谷店\n"
        "ご利用金額：1,280円\n"
    )
    assert mailSystem.parse_mail_body(body) == {
        "datetime": "2026/09/06 12:34:56",
        "location": "セブンイレブン 渋谷店",
        "amount": 1280,
    }


def test_parse_mail_body_handles_missing_and_invalid_values():
    assert mailSystem.parse_mail_body("関係ない本文") == {
        "datetime": "N/A",
        "location": "N/A",
        "amount": "N/A",
    }
    assert mailSystem.parse_mail_body("ご利用金額：?,???円") == {
        "datetime": "N/A",
        "location": "N/A",
        "amount": "N/A",
    }


def test_find_target_row_handles_headers_total_rows_and_append_fallback():
    assert mailSystem.find_target_row([["H1"], ["H2"], ["", "", "", ""]]) == 3
    assert mailSystem.find_target_row([["H1"], ["H2"], ["", "合計", "=SUM(C3:C50)", ""], ["", "", "", ""]]) == 4
    assert mailSystem.find_target_row([["H1"], ["H2"], ["", "", "", "", "メモのみ"]]) == 3
    assert mailSystem.find_target_row([["H1"], ["H2"], ["2026/01/01", "店", "100", "食費"]]) is None
    assert mailSystem.find_target_row([["H1"], ["", "", "", ""]], header_rows=1) == 2
