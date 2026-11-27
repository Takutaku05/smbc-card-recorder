import datetime
import json
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


def test_legacy_list_format_migrates_to_timestamp_dict_on_load_and_save(tmp_path, monkeypatch):
    proc_file = tmp_path / "processed_message_ids.json"
    proc_file.write_text('["id-1", "id-2"]', encoding="utf-8")

    monkeypatch.setattr(mailSystem, "PROCESSED_IDS_FILE", str(proc_file))
    monkeypatch.setattr(mailSystem, "processed_ids", set())
    monkeypatch.setattr(mailSystem, "_processed_id_timestamps", {})

    mailSystem.load_processed_ids()
    assert mailSystem.processed_ids == {"id-1", "id-2"}

    mailSystem.processed_ids.add("id-3")
    mailSystem.save_processed_ids()

    saved = json.loads(proc_file.read_text(encoding="utf-8"))
    assert isinstance(saved, dict)
    assert set(saved.keys()) == {"id-1", "id-2", "id-3"}


def test_expired_entries_are_purged_beyond_retention_days(tmp_path, monkeypatch):
    proc_file = tmp_path / "processed_message_ids.json"
    now = datetime.datetime.now(datetime.timezone.utc)
    old_ts = (now - datetime.timedelta(days=120)).isoformat()
    recent_ts = (now - datetime.timedelta(days=5)).isoformat()
    proc_file.write_text(json.dumps({"old-id": old_ts, "recent-id": recent_ts}), encoding="utf-8")

    monkeypatch.setattr(mailSystem, "PROCESSED_IDS_FILE", str(proc_file))
    monkeypatch.setattr(mailSystem, "processed_ids", set())
    monkeypatch.setattr(mailSystem, "_processed_id_timestamps", {})

    mailSystem.load_processed_ids()
    assert mailSystem.processed_ids == {"recent-id"}

    mailSystem.save_processed_ids()
    saved = json.loads(proc_file.read_text(encoding="utf-8"))
    assert set(saved.keys()) == {"recent-id"}


def test_max_entries_cap_keeps_newest_ids(tmp_path, monkeypatch):
    proc_file = tmp_path / "processed_message_ids.json"
    now = datetime.datetime.now(datetime.timezone.utc)
    data = {
        f"id-{i}": (now - datetime.timedelta(minutes=10 - i)).isoformat()
        for i in range(5)
    }
    proc_file.write_text(json.dumps(data), encoding="utf-8")

    monkeypatch.setattr(mailSystem, "PROCESSED_IDS_FILE", str(proc_file))
    monkeypatch.setattr(mailSystem, "PROCESSED_IDS_MAX_ENTRIES", 3)
    monkeypatch.setattr(mailSystem, "processed_ids", set())
    monkeypatch.setattr(mailSystem, "_processed_id_timestamps", {})

    mailSystem.load_processed_ids()
    assert mailSystem.processed_ids == {"id-2", "id-3", "id-4"}
