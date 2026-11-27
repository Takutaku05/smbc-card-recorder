"""
`python -m src.mailSystem` / `python src/mailSystem.py` 実行時（__main__ ブロック）の追加回帰テスト。

実際の Google / Discord / ファイル書き込みには一切触れず、起動シーケンスの順序・
スケジュール登録・初回実行・メインループ・停止通知を検証する。
"""

import io
import json
import os
import runpy
from unittest.mock import MagicMock, patch

import pytest
import schedule as schedule_mod

import src.mailSystem as mailSystem


@pytest.fixture(autouse=True)
def _clear_schedule():
    schedule_mod.clear()
    yield
    schedule_mod.clear()


def _run_main(
    *,
    json_files=None,
    init_ok=True,
    sleep_effects=(None, KeyboardInterrupt),
    gmail=None,
):
    """
    __main__ ブロックを安全に実行するヘルパー。

    :param json_files: {ファイル名: 内容(str)}。存在するものとして読み込ませる state ファイル
    :return: 検証用モックをまとめた dict
    """
    json_files = json_files or {}
    real_open = open
    real_exists = os.path.exists
    events = []

    def _exists(path):
        name = os.path.basename(str(path))
        if name.endswith(".json"):
            return name in json_files
        return real_exists(path)

    def _open(file, mode="r", *args, **kwargs):
        name = os.path.basename(str(file))
        if "w" in mode:
            events.append(("write", name))
            return io.StringIO()
        if name in json_files:
            events.append(("read", name))
            return io.StringIO(json_files[name])
        return real_open(file, mode, *args, **kwargs)

    if gmail is None:
        gmail = MagicMock()
        gmail.users().messages().list().execute.return_value = {"messages": []}

    disco_cls = MagicMock()
    creds = MagicMock(valid=True)
    creds.to_json.return_value = "{}"
    flow = MagicMock()
    flow.run_local_server.return_value = creds

    def _flow_factory(*a, **k):
        events.append(("oauth", None))
        if not init_ok:
            raise FileNotFoundError
        return flow

    with (
        patch("dotenv.load_dotenv") as load_dotenv,
        patch("src.mailDisco.MailDisco", disco_cls),
        patch(
            "google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file",
            side_effect=_flow_factory,
        ),
        patch("googleapiclient.discovery.build", return_value=gmail),
        patch("gspread.service_account", return_value=MagicMock()),
        patch("os.path.exists", side_effect=_exists),
        patch("builtins.open", side_effect=_open),
        patch(
            "os.getenv",
            side_effect=lambda k, d=None: (
                "dummy" if k in ("SERVICE_ACCOUNT_FILE", "SPREADSHEET_ID") else d
            ),
        ),
        patch(
            "schedule.run_pending",
            side_effect=lambda: events.append(("run_pending", None)),
        ) as run_pending,
        patch("time.sleep", side_effect=list(sleep_effects)) as sleep,
    ):
        runpy.run_path(mailSystem.__file__, run_name="__main__")

    return {
        "events": events,
        "disco_messages": [c.args[0] for c in disco_cls.call_args_list],
        "disco_cls": disco_cls,
        "load_dotenv": load_dotenv,
        "run_pending": run_pending,
        "sleep": sleep,
        "gmail": gmail,
    }


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
        # 初回実行で Gmail 検索が1回行われ、state が書き込まれる
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
        # 1回目の sleep は初回実行後、その後ループで run_pending -> sleep を繰り返す
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
