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


def test_check_mail_job_paginates_all_pages_and_excludes_in_inbox(monkeypatch):
    list_calls = []

    def _list(**kwargs):
        list_calls.append(kwargs)
        page_token = kwargs.get("pageToken")
        if page_token is None:
            resp = {
                "messages": [{"id": "m4"}, {"id": "m3"}],
                "nextPageToken": "page-2",
            }
        else:
            resp = {"messages": [{"id": "m2"}, {"id": "m1"}]}
        return types.SimpleNamespace(execute=lambda **_kw: resp)

    service = MagicMock()
    service.users.return_value.messages.return_value.list.side_effect = _list
    service.users.return_value.messages.return_value.get.side_effect = (
        lambda **kwargs: types.SimpleNamespace(
            execute=lambda **_kw: {
                "payload": {"headers": [{"name": "Subject", "value": kwargs["id"]}]}
            }
        )
    )

    processed_order = []
    monkeypatch.setattr(mailSystem, "gc", MagicMock())
    monkeypatch.setattr(mailSystem, "gmail_service", service)
    monkeypatch.setattr(mailSystem, "processed_ids", set())
    monkeypatch.setattr(mailSystem, "get_last_run_time", lambda: None)
    monkeypatch.setattr(mailSystem, "save_current_run_time", MagicMock())
    monkeypatch.setattr(mailSystem, "save_processed_ids", MagicMock())
    monkeypatch.setattr(mailSystem, "get_email_body", lambda p: p["headers"][0]["value"])
    monkeypatch.setattr(mailSystem, "system", lambda body: processed_order.append(body) or True)

    mailSystem.check_mail_job()

    assert len(list_calls) == 2
    assert list_calls[0]["maxResults"] == 500
    assert "in:inbox" not in list_calls[0]["q"]
    assert "-in:trash -in:spam" in list_calls[0]["q"]
    assert list_calls[1]["pageToken"] == "page-2"
    assert processed_order == ["m1", "m2", "m3", "m4"]
