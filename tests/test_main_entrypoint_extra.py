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
        # 設定・認証の誤りは再起動しても直らないため、RestartPreventExitStatus 用の終了コード 2 で終了する
        assert result["exit_code"] == mailSystem.EXIT_CONFIG_FAILURE == 2
        result["run_pending"].assert_not_called()
        result["sleep"].assert_not_called()
        assert schedule_mod.jobs == []

    def test_sigterm_handler_only_sets_flag_without_io(self, tmp_path):
        """シグナルハンドラ内では状態保存・通知・例外送出を行わず、停止要求フラグを立てるだけ。"""
        mailSystem.processed_ids = {"sigterm_msg"}
        with (
            patch("src.mailSystem.mailDisco.MailDisco") as mock_disco,
            patch.object(mailSystem, "save_processed_ids") as mock_save,
        ):
            mailSystem._handle_sigterm(signal.SIGTERM, None)
        assert mailSystem._shutdown_requested is True
        mock_disco.assert_not_called()
        mock_save.assert_not_called()
        assert not (tmp_path / "processed_message_ids.json").exists()

    def test_sigterm_in_main_loop_stops_gracefully_saves_state_and_notifies(self):
        calls = {"n": 0}

        def _on_sleep(_seconds):
            calls["n"] += 1
            if calls["n"] == 2:
                # runpy で実行中の __main__ 名前空間に登録されたハンドラを呼ぶ（= SIGTERM 受信）
                signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)

        result = _run_main(on_sleep=_on_sleep)
        assert result["exit_code"] is None  # 正常停止（終了コード 0）

        # 1回目: 初回実行後の sleep、2回目: ループ内の sleep（ここで SIGTERM）→ ループを抜ける
        assert result["sleep"].call_count == 2
        assert result["run_pending"].call_count == 1
        assert result["disco_messages"][-1] == "--- メール監視システムを停止しました ---"
        # 停止時は systemd の停止タイムアウトを超えないよう 429 を再試行しない
        assert result["disco_cls"].call_args_list[-1].kwargs == {"max_retries": 0}
        writes = [e[1] for e in result["events"] if e[0] == "write"]
        assert writes[-2:] == ["processed_message_ids.json", "failed_message_attempts.json"]

    def test_failure_counts_are_loaded_at_startup(self):
        result = _run_main(json_files={"failed_message_attempts.json": json.dumps({"m": 2})})
        assert ("read", "failed_message_attempts.json") in result["events"]
