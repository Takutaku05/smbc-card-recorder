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


def test_check_mail_job_processes_messages_in_chronological_order(monkeypatch):
    service = MagicMock()
    # Gmail API は新着順（降順）で返す
    service.users.return_value.messages.return_value.list.return_value.execute.return_value = {
        "messages": [{"id": "newest"}, {"id": "middle"}, {"id": "oldest"}]
    }
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
    monkeypatch.setattr(
        mailSystem,
        "system",
        lambda body: processed_order.append(body) or True,
    )

    mailSystem.check_mail_job()
    assert processed_order == ["oldest", "middle", "newest"]
