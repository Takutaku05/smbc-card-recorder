import datetime
import json
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


def test_save_and_get_last_run_time_are_utc_aware(tmp_path, monkeypatch):
    last_run_file = tmp_path / "last_run_time.json"
    monkeypatch.setattr(mailSystem, "LAST_RUN_FILE", str(last_run_file))

    jst = datetime.timezone(datetime.timedelta(hours=9))
    sample_dt = datetime.datetime(2026, 9, 6, 21, 30, 0, tzinfo=jst)
    mailSystem.save_current_run_time(sample_dt)

    saved = json.loads(last_run_file.read_text(encoding="utf-8"))
    assert saved["last_run_time"].endswith("+00:00")

    loaded = mailSystem.get_last_run_time()
    assert loaded is not None
    assert loaded.tzinfo == datetime.timezone.utc
    assert int(loaded.timestamp()) == int(sample_dt.timestamp())


def test_legacy_naive_last_run_time_migrates_to_utc_aware(tmp_path, monkeypatch):
    last_run_file = tmp_path / "last_run_time.json"
    naive_iso = "2026-09-06T12:00:00"
    last_run_file.write_text(json.dumps({"last_run_time": naive_iso}), encoding="utf-8")
    monkeypatch.setattr(mailSystem, "LAST_RUN_FILE", str(last_run_file))

    loaded = mailSystem.get_last_run_time()
    expected = datetime.datetime.fromisoformat(naive_iso).astimezone(datetime.timezone.utc)
    assert loaded == expected
    assert loaded.tzinfo == datetime.timezone.utc


def test_check_mail_job_applies_overlap_margin_and_saves_utc_time(monkeypatch):
    captured_queries = []
    saved_times = []

    def _list(**kwargs):
        captured_queries.append(kwargs["q"])
        return types.SimpleNamespace(execute=lambda **_kw: {"messages": []})

    service = MagicMock()
    service.users.return_value.messages.return_value.list.side_effect = _list

    last_run_utc = datetime.datetime(2026, 9, 6, 10, 0, 0, tzinfo=datetime.timezone.utc)
    expected_after = int(last_run_utc.timestamp()) - mailSystem.LAST_RUN_OVERLAP_SECONDS

    monkeypatch.setattr(mailSystem, "gc", MagicMock())
    monkeypatch.setattr(mailSystem, "gmail_service", service)
    monkeypatch.setattr(mailSystem, "get_last_run_time", lambda: last_run_utc)
    monkeypatch.setattr(mailSystem, "save_current_run_time", lambda dt: saved_times.append(dt))
    monkeypatch.setattr(mailSystem, "save_processed_ids", MagicMock())

    mailSystem.check_mail_job()

    assert f"after:{expected_after}" in captured_queries[0]
    assert len(saved_times) == 1
    assert saved_times[0].tzinfo == datetime.timezone.utc
