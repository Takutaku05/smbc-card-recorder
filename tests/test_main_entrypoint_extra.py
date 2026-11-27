"""
`python -m src.mailSystem` / `python src/mailSystem.py` 実行時（__main__ ブロック）の追加回帰テスト。
"""

import json
import signal
from unittest.mock import MagicMock, patch

import pytest
import schedule as schedule_mod

import src.mailSystem as mailSystem
from tests.conftest import run_main_block as _run_main


@pytest.fixture(autouse=True)
def _clear_schedule():
    schedule_mod.clear()
    yield
    schedule_mod.clear()


class TestMainEntrypointExtra:
    def test_dotenv_is_loaded_on_import(self):
        result = _run_main()
        result["load_dotenv"].assert_called_once_with()

    def test_processed_ids_loaded_before_service_initialization(self):
        result = _run_main(json_files={"processed_message_ids.json": "[]"})
        order = [e for e in result["events"] if e[0] in ("read", "oauth")]
        assert order[0] == ("read", "processed_message_ids.json")
        assert ("oauth", None) in order

    def test_previously_processed_ids_are_skipped_in_initial_run(self):
        gmail = MagicMock()
        gmail.users().messages().list().execute.return_value = {
            "messages": [{"id": "seen"}]
        }
        _run_main(
            json_files={"processed_message_ids.json": json.dumps(["seen"])}, gmail=gmail
        )
        gmail.users().messages().get.assert_not_called()

    def test_startup_sequence_and_notifications(self):
        result = _run_main()
        assert result["disco_messages"] == [
            "--- メール監視システムを起動しました ---",
            "--- メール監視システムを停止しました ---",
        ]

    def test_initial_check_runs_once_immediately(self):
        result = _run_main()
        result["gmail"].users().messages().list().execute.assert_called_once_with(
            num_retries=3
        )
        writes = [e[1] for e in result["events"] if e[0] == "write"]
        assert "last_run_time.json" in writes
        assert "processed_message_ids.json" in writes

    def test_jobs_are_hourly_at_00_and_30_and_run_check_mail_job(self):
        _run_main()
        assert len(schedule_mod.jobs) == 2
        for job in schedule_mod.jobs:
            assert job.unit == "hours"
            assert job.interval == 1
            assert job.job_func.func.__name__ == "check_mail_job"
        assert sorted(str(j.at_time) for j in schedule_mod.jobs) == [
            "00:00:00",
            "00:30:00",
        ]

    def test_main_loop_calls_run_pending_every_second_until_interrupt(self):
        result = _run_main(sleep_effects=(None, None, None, KeyboardInterrupt))
        assert result["run_pending"].call_count == 3
        assert all(c.args == (1,) for c in result["sleep"].call_args_list)

    def test_failed_initialization_sends_failure_only_and_does_not_loop(self):
        result = _run_main(init_ok=False)
        assert result["disco_messages"] == [
            "❌ サービスの初期化に失敗しました。プログラムを終了します。"
        ]
        result["run_pending"].assert_not_called()
        result["sleep"].assert_not_called()
        assert schedule_mod.jobs == []

    def test_sigterm_handler_saves_state_notifies_and_exits_zero(self, tmp_path):
        mailSystem.processed_ids = {"sigterm_msg"}
        with (
            patch("src.mailSystem.mailDisco.MailDisco") as mock_disco,
            pytest.raises(SystemExit) as exc_info,
        ):
            mailSystem._handle_sigterm(signal.SIGTERM, None)
        assert exc_info.value.code == 0
        mock_disco.assert_called_once_with("--- メール監視システムを停止しました ---")
        saved = json.loads(
            (tmp_path / "processed_message_ids.json").read_text(encoding="utf-8")
        )
        assert "sigterm_msg" in saved
