import sys
import types

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

from src import mailSystem


def _parse_only(monkeypatch, body: str):
    captured = {}

    def _fake_sheet(email_time, location, money):
        captured["result"] = (email_time, location, money)
        return True

    monkeypatch.setattr(mailSystem, "sheet", _fake_sheet)
    assert mailSystem.system(body) is True
    return captured["result"]


@pytest.mark.parametrize(
    "amount_line,expected_amount",
    [
        ("ご利用金額：1,234円", 1234),
        ("ご利用金額：1,234 円", 1234),
        ("利用金額:500円", 500),
        ("ご利用金額：-1,000円", -1000),
        ("ご利用金額：−1,000円", -1000),
        ("ご利用金額：－1,000円", -1000),
        ("ご利用金額：▲1,000円", -1000),
        ("ご利用金額：△ 1,000 円", -1000),
        ("ご利用金額：?,???円", "N/A"),
        ("ご利用金額：1-234円", "N/A"),
        ("ご利用金額：1,,000円", "N/A"),
        ("ご利用金額：,円", "N/A"),
    ],
)
def test_amount_parsing_and_na_fallback(monkeypatch, amount_line, expected_amount):
    body = f"ご利用日時：2026/01/05 12:34\nご利用先：テスト店舗\n{amount_line}"
    email_time, location, money = _parse_only(monkeypatch, body)
    assert email_time == "2026/01/05 12:34"
    assert location == "テスト店舗"
    assert money == expected_amount
    if expected_amount != "N/A":
        assert isinstance(money, int)


@pytest.mark.parametrize(
    "date_line,expected_dt,expected_sheet",
    [
        ("ご利用日時：2025/09/05 12:34", "2025/09/05 12:34", "9月"),
        ("ご利用日時：2025/9/5 9:34", "2025/9/5 9:34", "9月"),
        ("利用日: 2025-09-05　12:34:56", "2025/09/05 12:34:56", "9月"),
        ("ご利用日時：2025/9/5　08:05:09", "2025/9/5 08:05:09", "9月"),
    ],
)
def test_datetime_parsing_variations_and_timesheet(monkeypatch, date_line, expected_dt, expected_sheet):
    body = f"{date_line}\nご利用先：テスト店舗\nご利用金額：1,000円"
    email_time, _location, money = _parse_only(monkeypatch, body)
    assert email_time == expected_dt
    assert money == 1000
    assert mailSystem.timesheet(email_time) == expected_sheet
