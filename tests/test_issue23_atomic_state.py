import json
import sys
import types
from unittest.mock import MagicMock

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


def test_atomic_write_json_writes_cleanly_and_cleans_tmp(tmp_path):
    target = tmp_path / "state" / "data.json"
    mailSystem._atomic_write_json(str(target), ["id1", "id2"])

    assert json.loads(target.read_text(encoding="utf-8")) == ["id1", "id2"]
    assert not (tmp_path / "state" / "data.json.tmp").exists()


def test_atomic_write_json_preserves_existing_file_on_error(tmp_path, monkeypatch):
    target = tmp_path / "data.json"
    target.write_text('["old"]', encoding="utf-8")

    def _boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(mailSystem.json, "dump", _boom)

    with pytest.raises(OSError):
        mailSystem._atomic_write_json(str(target), ["new"])

    assert target.read_text(encoding="utf-8") == '["old"]'
    assert not (tmp_path / "data.json.tmp").exists()


def test_corrupted_state_files_notify_discord_and_recover(tmp_path, monkeypatch):
    last_run = tmp_path / "last_run_time.json"
    proc_ids = tmp_path / "processed_message_ids.json"
    last_run.write_text("{corrupt", encoding="utf-8")
    proc_ids.write_text("{corrupt", encoding="utf-8")

    sent = []

    class _Recorder:
        def __init__(self, msg):
            self.msg = msg

        def send(self):
            sent.append(self.msg)

    monkeypatch.setattr(mailSystem, "LAST_RUN_FILE", str(last_run))
    monkeypatch.setattr(mailSystem, "PROCESSED_IDS_FILE", str(proc_ids))
    monkeypatch.setattr(mailSystem, "processed_ids", {"preexisting"})
    monkeypatch.setattr(mailSystem.mailDisco, "MailDisco", _Recorder)

    assert mailSystem.get_last_run_time() is None
    mailSystem.load_processed_ids()
    assert mailSystem.processed_ids == set()
    assert len(sent) == 2


def test_check_mail_job_persists_processed_id_immediately_per_message(tmp_path, monkeypatch):
    proc_ids_file = tmp_path / "processed_message_ids.json"

    service = MagicMock()
    service.users.return_value.messages.return_value.list.return_value.execute.return_value = {
        "messages": [{"id": "m2"}, {"id": "m1"}]
    }
    service.users.return_value.messages.return_value.get.side_effect = (
        lambda **kwargs: types.SimpleNamespace(
            execute=lambda **_kw: {
                "payload": {"headers": [{"name": "Subject", "value": kwargs["id"]}]}
            }
        )
    )

    def _system(body):
        if body == "m2":
            raise KeyboardInterrupt("interrupted on 2nd message")
        return True

    monkeypatch.setattr(mailSystem, "PROCESSED_IDS_FILE", str(proc_ids_file))
    monkeypatch.setattr(mailSystem, "gc", MagicMock())
    monkeypatch.setattr(mailSystem, "gmail_service", service)
    monkeypatch.setattr(mailSystem, "processed_ids", set())
    monkeypatch.setattr(mailSystem, "get_last_run_time", lambda: None)
    monkeypatch.setattr(mailSystem, "save_current_run_time", MagicMock())
    monkeypatch.setattr(mailSystem, "get_email_body", lambda p: p["headers"][0]["value"])
    monkeypatch.setattr(mailSystem, "system", _system)

    with pytest.raises(KeyboardInterrupt):
        mailSystem.check_mail_job()

    assert json.loads(proc_ids_file.read_text(encoding="utf-8")) == ["m1"]
