"""
initialize_services() / check_mail_job() の追加回帰テスト。

ミューテーションテストで「コードを壊してもテストが通ってしまった」箇所
（API呼び出し引数、リトライ回数、早期return、状態保存のタイミング、運用ログ）を固定する。
"""

import datetime
import json
import logging
import types
from unittest.mock import MagicMock, patch

import pytest
from google.auth.transport.requests import Request
from googleapiclient.errors import HttpError

import src.mailSystem as mailSystem
from tests.conftest import FixedDateTime, encode_b64url

LOGGER = "smbc_card_recorder.src.mailSystem"
QUERY = 'in:inbox subject:"ご利用のお知らせ【三井住友カード】"'


def _http_error(status=500):
    resp = MagicMock(status=status, reason="err")
    return HttpError(resp=resp, content=b"{}")


def _msg(body_text="本文", headers=None):
    return {
        "payload": {
            "headers": headers
            if headers is not None
            else [{"name": "Subject", "value": "件名"}],
            "body": {"data": encode_b64url(body_text)},
        }
    }


def _gmail(message_ids, details=None):
    """list() が message_ids を返し、get() が details[id] を返す Gmail モック。"""
    gmail = MagicMock()
    gmail.users().messages().list().execute.return_value = {
        "messages": [{"id": i} for i in message_ids]
    }
    details = details or {}

    def _get(userId, id, format):  # Gmail API の引数名に合わせる
        req = MagicMock()
        value = details.get(id, _msg())
        if isinstance(value, Exception):
            req.execute.side_effect = value
        else:
            req.execute.return_value = value
        return req

    gmail.users().messages().get.side_effect = _get
    # 上の呼び出しで記録された call をリセットして、テスト本体の呼び出しだけを見る
    gmail.users().messages().list.reset_mock()
    return gmail


def _error_records(caplog):
    return [r for r in caplog.records if r.levelno >= logging.ERROR]


# ---------------------------------------------------------------------------
# initialize_services
# ---------------------------------------------------------------------------
class TestInitializeServicesExtra:
    def _token(self, tmp_path, text="{}"):
        (tmp_path / "gmailtoken.json").write_text(text, encoding="utf-8")

    def test_valid_token_is_not_rewritten(self, tmp_path):
        self._token(tmp_path, '{"original": true}')
        creds = MagicMock(valid=True)
        with (
            patch(
                "src.mailSystem.GmailCredentials.from_authorized_user_file",
                return_value=creds,
            ),
            patch("src.mailSystem.build"),
            patch("src.mailSystem.gspread.service_account"),
        ):
            assert mailSystem.initialize_services() is True
        creds.to_json.assert_not_called()
        creds.refresh.assert_not_called()
        assert (tmp_path / "gmailtoken.json").read_text(
            encoding="utf-8"
        ) == '{"original": true}'

    def test_refresh_is_called_with_google_request_object(self, tmp_path):
        self._token(tmp_path)
        creds = MagicMock(valid=False, expired=True, refresh_token="r")
        creds.to_json.return_value = "{}"
        with (
            patch(
                "src.mailSystem.GmailCredentials.from_authorized_user_file",
                return_value=creds,
            ),
            patch("src.mailSystem.build"),
            patch("src.mailSystem.gspread.service_account"),
        ):
            mailSystem.initialize_services()
        (arg,), _ = creds.refresh.call_args
        assert isinstance(arg, Request)

    def test_refresh_failure_does_not_touch_token_or_build_services(
        self, tmp_path, caplog
    ):
        self._token(tmp_path, '{"keep": 1}')
        creds = MagicMock(valid=False, expired=True, refresh_token="r")
        creds.refresh.side_effect = RuntimeError("invalid_grant")
        with (
            patch(
                "src.mailSystem.GmailCredentials.from_authorized_user_file",
                return_value=creds,
            ),
            patch("src.mailSystem.build") as mock_build,
            patch("src.mailSystem.gspread.service_account") as mock_sa,
            caplog.at_level(logging.ERROR, logger=LOGGER),
        ):
            assert mailSystem.initialize_services() is False
        mock_build.assert_not_called()
        mock_sa.assert_not_called()
        assert (tmp_path / "gmailtoken.json").read_text(
            encoding="utf-8"
        ) == '{"keep": 1}'
        assert mailSystem.gmail_service is None and mailSystem.gc is None
        # スタックトレース付きでログされ、再認証手順も案内される
        errors = _error_records(caplog)
        assert errors, "ERROR ログが出力されていない"
        assert errors[0].exc_info and errors[0].exc_info[0] is not None
        assert "gmailtoken.json' を削除して再認証してください" in caplog.text

    @pytest.mark.parametrize(
        "creds_kwargs",
        [
            {
                "valid": False,
                "expired": False,
                "refresh_token": "r",
            },  # 失効していないが無効（取り消し等）
            {
                "valid": False,
                "expired": True,
                "refresh_token": None,
            },  # リフレッシュトークンなし
            {"valid": False, "expired": True, "refresh_token": ""},
        ],
    )
    def test_invalid_token_without_refresh_path_runs_oauth_flow(
        self, tmp_path, creds_kwargs
    ):
        self._token(tmp_path)
        old_creds = MagicMock(**creds_kwargs)
        new_creds = MagicMock(valid=True)
        new_creds.to_json.return_value = '{"new": 1}'
        flow = MagicMock()
        flow.run_local_server.return_value = new_creds
        with (
            patch(
                "src.mailSystem.GmailCredentials.from_authorized_user_file",
                return_value=old_creds,
            ),
            patch(
                "src.mailSystem.InstalledAppFlow.from_client_secrets_file",
                return_value=flow,
            ),
            patch("src.mailSystem.build") as mock_build,
            patch("src.mailSystem.gspread.service_account"),
        ):
            assert mailSystem.initialize_services() is True
        old_creds.refresh.assert_not_called()
        mock_build.assert_called_once_with("gmail", "v1", credentials=new_creds)
        assert (tmp_path / "gmailtoken.json").read_text(
            encoding="utf-8"
        ) == '{"new": 1}'

    def test_oauth_flow_uses_config_credentials_json_and_readonly_scope(self):
        flow = MagicMock()
        flow.run_local_server.return_value = MagicMock(**{"to_json.return_value": "{}"})
        with (
            patch(
                "src.mailSystem.InstalledAppFlow.from_client_secrets_file",
                return_value=flow,
            ) as mock_from,
            patch("src.mailSystem.build"),
            patch("src.mailSystem.gspread.service_account"),
        ):
            mailSystem.initialize_services()
        expected_path = str(
            __import__("pathlib").Path(mailSystem.BASE_DIR)
            / "config"
            / "credentials.json"
        )
        mock_from.assert_called_once_with(
            expected_path, ["https://www.googleapis.com/auth/gmail.readonly"]
        )

    def test_token_file_not_loaded_when_missing(self):
        flow = MagicMock()
        flow.run_local_server.return_value = MagicMock(**{"to_json.return_value": "{}"})
        with (
            patch(
                "src.mailSystem.GmailCredentials.from_authorized_user_file"
            ) as mock_load,
            patch(
                "src.mailSystem.InstalledAppFlow.from_client_secrets_file",
                return_value=flow,
            ),
            patch("src.mailSystem.build"),
            patch("src.mailSystem.gspread.service_account"),
        ):
            mailSystem.initialize_services()
        mock_load.assert_not_called()

    def test_missing_credentials_json_does_not_create_token_file(self, tmp_path):
        with (
            patch(
                "src.mailSystem.InstalledAppFlow.from_client_secrets_file",
                side_effect=FileNotFoundError,
            ),
            patch("src.mailSystem.build") as mock_build,
        ):
            assert mailSystem.initialize_services() is False
        mock_build.assert_not_called()
        assert not (tmp_path / "gmailtoken.json").exists()

    def test_gmail_build_error_stops_before_gspread(self, tmp_path, caplog):
        self._token(tmp_path)
        with (
            patch(
                "src.mailSystem.GmailCredentials.from_authorized_user_file",
                return_value=MagicMock(valid=True),
            ),
            patch("src.mailSystem.build", side_effect=_http_error()),
            patch("src.mailSystem.gspread.service_account") as mock_sa,
            caplog.at_level(logging.ERROR, logger=LOGGER),
        ):
            assert mailSystem.initialize_services() is False
        mock_sa.assert_not_called()
        assert mailSystem.gmail_service is None
        errors = _error_records(caplog)
        assert errors, "ERROR ログが出力されていない"
        assert errors[0].exc_info and errors[0].exc_info[0] is not None

    def test_missing_service_account_env_does_not_call_gspread(
        self, tmp_path, monkeypatch
    ):
        self._token(tmp_path)
        monkeypatch.setattr(mailSystem, "SERVICE_ACCOUNT_FILE", "")
        with (
            patch(
                "src.mailSystem.GmailCredentials.from_authorized_user_file",
                return_value=MagicMock(valid=True),
            ),
            patch("src.mailSystem.build"),
            patch("src.mailSystem.gspread.service_account") as mock_sa,
        ):
            assert mailSystem.initialize_services() is False
        mock_sa.assert_not_called()
        assert mailSystem.gc is None

    def test_gspread_failure_leaves_gmail_initialized_but_gc_none(
        self, tmp_path, caplog
    ):
        self._token(tmp_path)
        gmail = MagicMock()
        with (
            patch(
                "src.mailSystem.GmailCredentials.from_authorized_user_file",
                return_value=MagicMock(valid=True),
            ),
            patch("src.mailSystem.build", return_value=gmail),
            patch(
                "src.mailSystem.gspread.service_account",
                side_effect=PermissionError("denied"),
            ),
            caplog.at_level(logging.ERROR, logger=LOGGER),
        ):
            assert mailSystem.initialize_services() is False
        assert mailSystem.gmail_service is gmail
        assert mailSystem.gc is None
        errors = _error_records(caplog)
        assert errors, "ERROR ログが出力されていない"
        assert errors[0].exc_info and errors[0].exc_info[0] is not None
        assert "編集者権限" in caplog.text

    def test_success_logs_start_and_completion(self, tmp_path, caplog):
        self._token(tmp_path)
        with (
            patch(
                "src.mailSystem.GmailCredentials.from_authorized_user_file",
                return_value=MagicMock(valid=True),
            ),
            patch("src.mailSystem.build"),
            patch("src.mailSystem.gspread.service_account"),
            caplog.at_level(logging.INFO, logger=LOGGER),
        ):
            mailSystem.initialize_services()
        assert "--- サービス初期化開始 ---" in caplog.text
        assert "Gmail APIサービスに接続しました。" in caplog.text
        assert "Google スプレッドシートAPIに接続しました。" in caplog.text
        assert "--- サービス初期化完了 ---" in caplog.text


# ---------------------------------------------------------------------------
# check_mail_job
# ---------------------------------------------------------------------------
class TestCheckMailJobExtra:
    @pytest.mark.parametrize(
        "gmail_ready,gc_ready", [(False, True), (True, False), (False, False)]
    )
    def test_early_return_touches_nothing(self, tmp_path, gmail_ready, gc_ready):
        gmail = MagicMock()
        mailSystem.gmail_service = gmail if gmail_ready else None
        mailSystem.gc = MagicMock() if gc_ready else None
        with (
            patch.object(mailSystem, "get_last_run_time") as mock_last,
            patch("src.mailSystem.mailDisco.MailDisco") as mock_disco,
        ):
            mailSystem.check_mail_job()
        mock_last.assert_not_called()
        gmail.users.assert_not_called()
        mock_disco.assert_not_called()
        assert not (tmp_path / "last_run_time.json").exists()
        assert not (tmp_path / "processed_message_ids.json").exists()

    def test_list_and_get_use_expected_arguments_and_retries(self):
        gmail = _gmail(["m1"])
        mailSystem.gmail_service = gmail
        mailSystem.gc = MagicMock()
        with patch.object(mailSystem, "system"):
            mailSystem.check_mail_job()

        gmail.users().messages().list.assert_called_once_with(userId="me", q=QUERY)
        gmail.users().messages().list().execute.assert_called_once_with(num_retries=3)
        gmail.users().messages().get.assert_called_once_with(
            userId="me", id="m1", format="full"
        )

    def test_get_execute_uses_three_retries(self):
        gmail = MagicMock()
        gmail.users().messages().list().execute.return_value = {
            "messages": [{"id": "m1"}]
        }
        req = MagicMock()
        req.execute.return_value = _msg()
        gmail.users().messages().get.return_value = req
        mailSystem.gmail_service = gmail
        mailSystem.gc = MagicMock()
        with patch.object(mailSystem, "system"):
            mailSystem.check_mail_job()
        req.execute.assert_called_once_with(num_retries=3)

    def test_after_timestamp_is_integer_epoch_seconds(self):
        last = datetime.datetime(2026, 11, 20, 12, 0, 0, 987654)
        mailSystem.save_current_run_time(last)
        gmail = _gmail([])
        mailSystem.gmail_service = gmail
        mailSystem.gc = MagicMock()
        mailSystem.check_mail_job()
        q = gmail.users().messages().list.call_args.kwargs["q"]
        assert q == f"{QUERY} after:{int(last.timestamp())}"
        assert "." not in q.split("after:")[1]

    def test_timezone_aware_last_run_time_uses_absolute_epoch(self):
        jst = datetime.timezone(datetime.timedelta(hours=9))
        last = datetime.datetime(2026, 11, 20, 21, 0, 0, tzinfo=jst)
        mailSystem.save_current_run_time(last)
        gmail = _gmail([])
        mailSystem.gmail_service = gmail
        mailSystem.gc = MagicMock()
        mailSystem.check_mail_job()
        expected = int(
            datetime.datetime(
                2026, 11, 20, 12, 0, 0, tzinfo=datetime.timezone.utc
            ).timestamp()
        )
        assert (
            gmail.users()
            .messages()
            .list.call_args.kwargs["q"]
            .endswith(f"after:{expected}")
        )

    def test_saved_run_time_is_captured_before_processing(self, tmp_path, monkeypatch):
        """last_run_time にはジョブ開始時点の now() が保存される（処理完了時刻ではない）。"""
        fake_dt_module = types.SimpleNamespace(datetime=FixedDateTime)
        monkeypatch.setattr(mailSystem, "datetime", fake_dt_module)

        def _slow_system(_body):
            FixedDateTime.fixed_now = datetime.datetime(2026, 11, 27, 9, 45, 0)

        monkeypatch.setattr(
            FixedDateTime, "fixed_now", datetime.datetime(2026, 11, 27, 9, 30, 0)
        )
        mailSystem.gmail_service = _gmail(["m1"])
        mailSystem.gc = MagicMock()
        with patch.object(mailSystem, "system", side_effect=_slow_system):
            mailSystem.check_mail_job()

        saved = json.loads(
            (tmp_path / "last_run_time.json").read_text(encoding="utf-8")
        )
        assert saved == {"last_run_time": "2026-11-27T09:30:00"}

    def test_state_is_saved_even_when_every_message_fails(self, tmp_path):
        mailSystem.gmail_service = _gmail(["bad"], {"bad": _http_error(500)})
        mailSystem.gc = MagicMock()
        with patch("src.mailSystem.mailDisco.MailDisco") as mock_disco:
            mailSystem.check_mail_job()
        assert (tmp_path / "last_run_time.json").exists()
        assert (
            json.loads(
                (tmp_path / "processed_message_ids.json").read_text(encoding="utf-8")
            )
            == []
        )
        # メール単位のエラーでは Discord 通知しない
        mock_disco.assert_not_called()

    def test_no_discord_notification_on_successful_run_without_messages(self):
        mailSystem.gmail_service = _gmail([])
        mailSystem.gc = MagicMock()
        with patch("src.mailSystem.mailDisco.MailDisco") as mock_disco:
            mailSystem.check_mail_job()
        mock_disco.assert_not_called()

    def test_messages_key_missing_is_treated_as_no_messages(self, caplog):
        gmail = MagicMock()
        gmail.users().messages().list().execute.return_value = {"resultSizeEstimate": 0}
        mailSystem.gmail_service = gmail
        mailSystem.gc = MagicMock()
        with caplog.at_level(logging.INFO, logger=LOGGER):
            mailSystem.check_mail_job()
        gmail.users().messages().get.assert_not_called()
        assert "新しい対象メールは見つかりませんでした。" in caplog.text

    def test_messages_processed_in_api_order(self):
        mailSystem.gmail_service = _gmail(
            ["c", "a", "b"],
            {
                "c": _msg("C"),
                "a": _msg("A"),
                "b": _msg("B"),
            },
        )
        mailSystem.gc = MagicMock()
        with patch.object(mailSystem, "system") as mock_system:
            mailSystem.check_mail_job()
        assert [c.args[0] for c in mock_system.call_args_list] == ["C", "A", "B"]

    def test_duplicate_ids_in_same_listing_are_processed_once(self):
        """現行仕様: 同一リスト内の重複IDはループ中に processed_ids へ追加されるので2回目はスキップ。"""
        mailSystem.gmail_service = _gmail(["dup", "dup"])
        mailSystem.gc = MagicMock()
        with patch.object(mailSystem, "system") as mock_system:
            mailSystem.check_mail_job()
        assert mock_system.call_count == 1

    def test_processed_ids_file_contains_list_of_ids(self, tmp_path):
        mailSystem.gmail_service = _gmail(["x1", "x2"])
        mailSystem.gc = MagicMock()
        with patch.object(mailSystem, "system"):
            mailSystem.check_mail_job()
        data = json.loads(
            (tmp_path / "processed_message_ids.json").read_text(encoding="utf-8")
        )
        assert isinstance(data, list)
        assert sorted(data) == ["x1", "x2"]

    # --- ログ（運用時の唯一の手掛かりなので文言を固定する） ---
    @pytest.mark.parametrize(
        "headers,expected",
        [
            (
                [{"name": "Subject", "value": "ご利用のお知らせ【三井住友カード】"}],
                "件名: ご利用のお知らせ【三井住友カード】",
            ),
            ([{"name": "SUBJECT", "value": "大文字"}], "件名: 大文字"),
            ([{"name": "subject", "value": "小文字"}], "件名: 小文字"),
            (
                [
                    {"name": "Subject", "value": "1つ目"},
                    {"name": "Subject", "value": "2つ目"},
                ],
                "件名: 1つ目",
            ),
            ([{"name": "From", "value": "x"}], "件名: 件名なし"),
            ([], "件名: 件名なし"),
        ],
    )
    def test_subject_header_is_logged(self, caplog, headers, expected):
        mailSystem.gmail_service = _gmail(["m"], {"m": _msg("本文", headers=headers)})
        mailSystem.gc = MagicMock()
        with (
            patch.object(mailSystem, "system"),
            caplog.at_level(logging.INFO, logger=LOGGER),
        ):
            mailSystem.check_mail_job()
        subject_lines = [
            r.getMessage()
            for r in caplog.records
            if r.getMessage().startswith("件名: ")
        ]
        assert subject_lines == [expected]

    def test_logs_new_message_count(self, caplog):
        mailSystem.processed_ids = {"old"}
        mailSystem.gmail_service = _gmail(["old", "n1", "n2"])
        mailSystem.gc = MagicMock()
        with (
            patch.object(mailSystem, "system"),
            caplog.at_level(logging.INFO, logger=LOGGER),
        ):
            mailSystem.check_mail_job()
        messages = [r.getMessage() for r in caplog.records]
        assert "3 件の候補メールが見つかりました。処理を開始します。" in messages
        assert "2 件の新しいメールの処理が完了しました。" in messages
        assert "処理中のメールID: n1" in messages
        assert "処理中のメールID: old" not in messages
        assert "メールID n1 の処理が完了し、処理済みとしてマークしました。" in messages

    def test_failed_messages_still_count_as_attempted(self, caplog):
        mailSystem.gmail_service = _gmail(["e1"], {"e1": _http_error(500)})
        mailSystem.gc = MagicMock()
        with caplog.at_level(logging.INFO, logger=LOGGER):
            mailSystem.check_mail_job()
        assert "1 件の新しいメールの処理が完了しました。" in [
            r.getMessage() for r in caplog.records
        ]

    def test_logs_when_all_candidates_already_processed(self, caplog):
        mailSystem.processed_ids = {"a", "b"}
        mailSystem.gmail_service = _gmail(["a", "b"])
        mailSystem.gc = MagicMock()
        with (
            patch.object(mailSystem, "system") as mock_system,
            caplog.at_level(logging.INFO, logger=LOGGER),
        ):
            mailSystem.check_mail_job()
        mock_system.assert_not_called()
        messages = [r.getMessage() for r in caplog.records]
        assert "新しい未処理メールはありませんでした（処理済みを除く）。" in messages
        assert not any("件の新しいメールの処理が完了しました" in m for m in messages)

    def test_logs_search_mode_first_run_vs_incremental(self, caplog):
        mailSystem.gmail_service = _gmail([])
        mailSystem.gc = MagicMock()
        with caplog.at_level(logging.INFO, logger=LOGGER):
            mailSystem.check_mail_job()
        assert (
            "初回チェックまたは日時不明。対象件名の全メールを検索候補とします。"
            in caplog.text
        )

        caplog.clear()
        mailSystem.save_current_run_time(datetime.datetime(2026, 11, 20, 8, 5, 3))
        with caplog.at_level(logging.INFO, logger=LOGGER):
            mailSystem.check_mail_job()
        assert "最終チェック日時 (2026-11-20 08:05:03) 以降を検索。" in caplog.text

    def test_logs_start_and_end_banners_with_timestamp(self, caplog, monkeypatch):
        monkeypatch.setattr(
            mailSystem, "datetime", types.SimpleNamespace(datetime=FixedDateTime)
        )
        monkeypatch.setattr(
            FixedDateTime, "fixed_now", datetime.datetime(2026, 11, 27, 9, 30, 5)
        )
        mailSystem.gmail_service = _gmail([])
        mailSystem.gc = MagicMock()
        with caplog.at_level(logging.INFO, logger=LOGGER):
            mailSystem.check_mail_job()
        messages = [r.getMessage() for r in caplog.records]
        assert messages[0] == "--- 2026-11-27 09:30:05: 定時メールチェック実行 ---"
        assert messages[-1] == "--- 2026-11-27 09:30:05: メールチェック完了 ---"

    def test_end_banner_logged_even_after_api_error(self, caplog):
        gmail = MagicMock()
        gmail.users().messages().list().execute.side_effect = _http_error(500)
        mailSystem.gmail_service = gmail
        mailSystem.gc = MagicMock()
        with (
            patch("src.mailSystem.mailDisco.MailDisco"),
            caplog.at_level(logging.INFO, logger=LOGGER),
        ):
            mailSystem.check_mail_job()
        assert caplog.records[-1].getMessage().endswith(": メールチェック完了 ---")

    def test_empty_body_logs_warning(self, caplog):
        mailSystem.gmail_service = _gmail(
            ["e"], {"e": {"payload": {"headers": [], "parts": []}}}
        )
        mailSystem.gc = MagicMock()
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            mailSystem.check_mail_job()
        assert (
            "メールID e の本文が見つかりませんでした。スキップします。" in caplog.text
        )

    @pytest.mark.parametrize(
        "failure,expected_text,has_exc_info",
        [
            (_http_error(503), "メールID m の詳細取得中にエラー", False),
            (RuntimeError("boom"), "メールID m の処理中に予期せぬエラー", True),
        ],
    )
    def test_per_message_error_logging(
        self, caplog, failure, expected_text, has_exc_info
    ):
        if isinstance(failure, HttpError):
            mailSystem.gmail_service = _gmail(["m"], {"m": failure})
            ctx = patch.object(mailSystem, "system")
        else:
            mailSystem.gmail_service = _gmail(["m"])
            ctx = patch.object(mailSystem, "system", side_effect=failure)
        mailSystem.gc = MagicMock()
        with ctx, caplog.at_level(logging.ERROR, logger=LOGGER):
            mailSystem.check_mail_job()
        records = _error_records(caplog)
        assert len(records) == 1
        assert expected_text in records[0].getMessage()
        assert bool(records[0].exc_info) is has_exc_info

    @pytest.mark.parametrize(
        "failure,discord_message",
        [
            (_http_error(403), "❌ Gmail API呼び出し中にエラーが発生しました"),
            (RuntimeError("x"), "ジョブ実行で予期せぬエラーが発生しました"),
        ],
    )
    def test_job_level_errors_logged_with_traceback_and_ids_not_saved(
        self, tmp_path, caplog, failure, discord_message
    ):
        gmail = MagicMock()
        gmail.users().messages().list().execute.side_effect = failure
        mailSystem.gmail_service = gmail
        mailSystem.gc = MagicMock()
        with (
            patch("src.mailSystem.mailDisco.MailDisco") as mock_disco,
            caplog.at_level(logging.ERROR, logger=LOGGER),
        ):
            mailSystem.check_mail_job()
        records = _error_records(caplog)
        assert len(records) == 1
        assert records[0].exc_info and records[0].exc_info[0] is not None
        mock_disco.assert_called_once_with(discord_message)
        assert not (tmp_path / "processed_message_ids.json").exists()


class TestLoggingDetailsInStateAndSheet:
    def test_load_processed_ids_logs_count(self, tmp_path, caplog):
        (tmp_path / "processed_message_ids.json").write_text(
            '["a","b","c"]', encoding="utf-8"
        )
        with caplog.at_level(logging.INFO, logger=LOGGER):
            mailSystem.load_processed_ids()
        assert "処理済みメールIDを 3 件ロードしました。" in caplog.text

    def test_load_processed_ids_missing_file_log_contains_path(self, tmp_path, caplog):
        with caplog.at_level(logging.INFO, logger=LOGGER):
            mailSystem.load_processed_ids()
        assert (
            f"処理済みメールIDファイル '{tmp_path / 'processed_message_ids.json'}' が見つかりません。"
            in caplog.text
        )

    def test_load_processed_ids_corrupt_file_log_contains_path(self, tmp_path, caplog):
        (tmp_path / "processed_message_ids.json").write_text("{", encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            mailSystem.load_processed_ids()
        assert (
            f"処理済みメールIDファイル '{tmp_path / 'processed_message_ids.json'}' が不正です。"
            in caplog.text
        )

    def test_system_logs_extraction_result(self, caplog):
        with (
            patch.object(mailSystem, "sheet"),
            caplog.at_level(logging.INFO, logger=LOGGER),
        ):
            mailSystem.system("利用日: 2026/05/01 10:00\n利用先: 店\n利用金額: 1,000円")
        assert (
            "抽出結果: 利用日=2026/05/01 10:00, 利用先=店, 利用金額=1000" in caplog.text
        )

    def test_system_does_not_warn_when_amount_simply_missing(self, caplog):
        with (
            patch.object(mailSystem, "sheet"),
            caplog.at_level(logging.WARNING, logger=LOGGER),
        ):
            mailSystem.system("利用日: 2026/05/01 10:00")
        assert "数値に変換できませんでした" not in caplog.text

    def test_system_warning_includes_raw_amount(self, caplog):
        with (
            patch.object(mailSystem, "sheet"),
            caplog.at_level(logging.WARNING, logger=LOGGER),
        ):
            mailSystem.system("利用金額: 1,-2円")
        assert "金額 '1,-2' を数値に変換できませんでした。" in caplog.text

    def test_timesheet_warning_includes_input(self, caplog):
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            mailSystem.timesheet("壊れた日付")
        assert "timesheet関数: 日付 '壊れた日付' の形式が不正です。" in caplog.text
