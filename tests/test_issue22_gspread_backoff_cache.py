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

    class _BackOffHTTPClient:
        pass

    sys.modules["gspread"] = types.SimpleNamespace(
        exceptions=types.SimpleNamespace(
            WorksheetNotFound=_WorksheetNotFound,
            SpreadsheetNotFound=_SpreadsheetNotFound,
        ),
        BackOffHTTPClient=_BackOffHTTPClient,
        service_account=MagicMock(),
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


def test_initialize_services_uses_backoff_http_client(tmp_path, monkeypatch):
    token_file = tmp_path / "gmailtoken.json"
    token_file.write_text("{}", encoding="utf-8")
    creds = MagicMock(valid=True)

    service_account_mock = MagicMock()
    backoff_cls = object()

    monkeypatch.setattr(mailSystem, "TOKEN_FILE", str(token_file))
    monkeypatch.setattr(mailSystem, "SERVICE_ACCOUNT_FILE", "dummy_sa.json")
    monkeypatch.setattr(mailSystem, "GmailCredentials", types.SimpleNamespace(from_authorized_user_file=lambda *_a: creds))
    monkeypatch.setattr(mailSystem, "build", lambda *_a, **_kw: MagicMock())
    monkeypatch.setattr(_gspread, "BackOffHTTPClient", backoff_cls, raising=False)
    monkeypatch.setattr(_gspread, "service_account", service_account_mock, raising=False)

    assert mailSystem.initialize_services() is True
    service_account_mock.assert_called_once_with(
        filename="dummy_sa.json",
        scopes=mailSystem.SCOPES_SHEET,
        http_client=backoff_cls,
    )


def test_sheet_caches_spreadsheet_and_worksheets(monkeypatch):
    ws_jan = MagicMock()
    ws_jan.get_all_values.return_value = [["H1"], ["H2"], ["", "", "", ""]]
    ws_feb = MagicMock()
    ws_feb.get_all_values.return_value = [["H1"], ["H2"], ["", "", "", ""]]

    spreadsheet = MagicMock()
    spreadsheet.worksheet.side_effect = lambda name: {"1月": ws_jan, "2月": ws_feb}[name]
    gc = MagicMock()
    gc.open_by_key.return_value = spreadsheet

    monkeypatch.setattr(mailSystem, "gc", gc)
    monkeypatch.setattr(mailSystem, "SPREADSHEET_ID", "sheet-1")
    monkeypatch.setattr(mailSystem, "_cached_gc", None)
    monkeypatch.setattr(mailSystem, "_cached_spreadsheet_id", None)
    monkeypatch.setattr(mailSystem, "_cached_spreadsheet", None)
    monkeypatch.setattr(mailSystem, "_worksheet_cache", {})
    monkeypatch.setattr(mailSystem.purpose, "Purpose", _DummyPurpose)
    monkeypatch.setattr(mailSystem.mailDisco, "MailDisco", MagicMock())

    assert mailSystem.sheet("2026/01/05 12:00", "店A", 100) is True
    assert mailSystem.sheet("2026/01/06 12:00", "店B", 200) is True
    assert mailSystem.sheet("2026/02/01 12:00", "店C", 300) is True

    assert gc.open_by_key.call_count == 1
    assert spreadsheet.worksheet.call_count == 2


def test_sheet_invalidates_cache_when_gc_or_spreadsheet_id_changes(monkeypatch):
    ws = MagicMock()
    ws.get_all_values.return_value = [["H1"], ["H2"], ["", "", "", ""]]

    spreadsheet = MagicMock()
    spreadsheet.worksheet.return_value = ws
    gc1 = MagicMock()
    gc1.open_by_key.return_value = spreadsheet
    gc2 = MagicMock()
    gc2.open_by_key.return_value = spreadsheet

    monkeypatch.setattr(mailSystem, "gc", gc1)
    monkeypatch.setattr(mailSystem, "SPREADSHEET_ID", "sheet-1")
    monkeypatch.setattr(mailSystem, "_cached_gc", None)
    monkeypatch.setattr(mailSystem, "_cached_spreadsheet_id", None)
    monkeypatch.setattr(mailSystem, "_cached_spreadsheet", None)
    monkeypatch.setattr(mailSystem, "_worksheet_cache", {})
    monkeypatch.setattr(mailSystem.purpose, "Purpose", _DummyPurpose)
    monkeypatch.setattr(mailSystem.mailDisco, "MailDisco", MagicMock())

    assert mailSystem.sheet("2026/01/05 12:00", "店A", 100) is True
    monkeypatch.setattr(mailSystem, "SPREADSHEET_ID", "sheet-2")
    assert mailSystem.sheet("2026/01/06 12:00", "店B", 200) is True
    assert gc1.open_by_key.call_count == 2

    monkeypatch.setattr(mailSystem, "gc", gc2)
    assert mailSystem.sheet("2026/01/07 12:00", "店C", 300) is True
    assert gc2.open_by_key.call_count == 1


def test_cache_cleared_after_write_failure():
    """書き込み失敗時はキャッシュを破棄し、次回は open_by_key から再取得する。"""
    import types

    import src.mailSystem as ms

    calls = {"open": 0}
    ws = types.SimpleNamespace(
        get_all_values=lambda: (_ for _ in ()).throw(RuntimeError("stale")),
        append_row=lambda row: (_ for _ in ()).throw(RuntimeError("stale")),
    )
    ss = types.SimpleNamespace(worksheet=lambda name: ws)

    def open_by_key(key):
        calls["open"] += 1
        return ss

    ms.gc = types.SimpleNamespace(open_by_key=open_by_key)
    ms._cached_gc = None
    ms._cached_spreadsheet = None
    ms._worksheet_cache = {}

    import unittest.mock as um
    with um.patch.object(ms.mailDisco, "MailDisco"), um.patch.object(ms.purpose, "Purpose"):
        assert ms.sheet("2026/11/27 10:00", "店", 100) is False
        assert ms._cached_spreadsheet is None
        assert ms._worksheet_cache == {}
        ms.sheet("2026/11/27 10:00", "店", 100)
    assert calls["open"] == 2
