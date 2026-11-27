import datetime
import json
import logging
from unittest.mock import MagicMock, patch

import pytest
from googleapiclient.errors import HttpError

import src.mailSystem as mailSystem
import src.purpose as purpose_module
from tests.conftest import encode_b64url


def _make_http_error(status: int = 500, reason: str = "Server Error") -> HttpError:
    resp = MagicMock()
    resp.status = status
    resp.reason = reason
    return HttpError(resp=resp, content=b'{"error": "boom"}')


class TestInitializeServices:
    def test_success_with_valid_existing_token(self, tmp_path):
        token_path = tmp_path / "gmailtoken.json"
        token_path.write_text("{}", encoding="utf-8")

        mock_creds = MagicMock(valid=True, expired=False)
        mock_gmail = MagicMock()
        mock_gc = MagicMock()

        with patch("src.mailSystem.GmailCredentials.from_authorized_user_file", return_value=mock_creds) as mock_from_file, \
             patch("src.mailSystem.build", return_value=mock_gmail) as mock_build, \
             patch("src.mailSystem.gspread.service_account", return_value=mock_gc) as mock_sa:
            result = mailSystem.initialize_services()

        assert result is True
        mock_from_file.assert_called_once_with(str(token_path), [mailSystem.SCOPES_GMAIL])
        mock_build.assert_called_once_with("gmail", "v1", credentials=mock_creds)
        mock_sa.assert_called_once_with(
            filename=mailSystem.SERVICE_ACCOUNT_FILE,
            scopes=mailSystem.SCOPES_SHEET,
        )
        assert mailSystem.gmail_service is mock_gmail
        assert mailSystem.gc is mock_gc

    def test_success_by_refreshing_expired_token_and_saving(self, tmp_path):
        token_path = tmp_path / "gmailtoken.json"
        token_path.write_text('{"old": true}', encoding="utf-8")

        mock_creds = MagicMock(valid=False, expired=True, refresh_token="refresh-tok")
        mock_creds.to_json.return_value = '{"refreshed": true}'

        with patch("src.mailSystem.GmailCredentials.from_authorized_user_file", return_value=mock_creds), \
             patch("src.mailSystem.build", return_value=MagicMock()), \
             patch("src.mailSystem.gspread.service_account", return_value=MagicMock()):
            result = mailSystem.initialize_services()

        assert result is True
        mock_creds.refresh.assert_called_once()
        assert token_path.read_text(encoding="utf-8") == '{"refreshed": true}'

    def test_returns_false_when_token_refresh_fails(self, tmp_path, caplog):
        token_path = tmp_path / "gmailtoken.json"
        token_path.write_text("{}", encoding="utf-8")

        mock_creds = MagicMock(valid=False, expired=True, refresh_token="refresh-tok")
        mock_creds.refresh.side_effect = RuntimeError("invalid_grant")

        with patch("src.mailSystem.GmailCredentials.from_authorized_user_file", return_value=mock_creds), \
             caplog.at_level(logging.ERROR, logger="smbc_card_recorder.src.mailSystem"):
            result = mailSystem.initialize_services()

        assert result is False
        assert "Gmailトークンのリフレッシュに失敗" in caplog.text

    def test_success_with_oauth_flow_when_no_token_file(self, tmp_path):
        mock_creds = MagicMock(valid=True)
        mock_creds.to_json.return_value = '{"new_token": true}'
        mock_flow = MagicMock()
        mock_flow.run_local_server.return_value = mock_creds

        with patch("src.mailSystem.InstalledAppFlow.from_client_secrets_file", return_value=mock_flow), \
             patch("src.mailSystem.build", return_value=MagicMock()), \
             patch("src.mailSystem.gspread.service_account", return_value=MagicMock()):
            result = mailSystem.initialize_services()

        assert result is True
        mock_flow.run_local_server.assert_called_once_with(port=0)
        assert (tmp_path / "gmailtoken.json").read_text(encoding="utf-8") == '{"new_token": true}'

    def test_returns_false_when_credentials_json_missing(self, caplog):
        with patch("src.mailSystem.InstalledAppFlow.from_client_secrets_file", side_effect=FileNotFoundError), \
             caplog.at_level(logging.ERROR, logger="smbc_card_recorder.src.mailSystem"):
            result = mailSystem.initialize_services()

        assert result is False
        assert "credentials.json" in caplog.text

    def test_returns_false_when_gmail_build_raises_http_error(self, tmp_path, caplog):
        (tmp_path / "gmailtoken.json").write_text("{}", encoding="utf-8")
        mock_creds = MagicMock(valid=True)

        with patch("src.mailSystem.GmailCredentials.from_authorized_user_file", return_value=mock_creds), \
             patch("src.mailSystem.build", side_effect=_make_http_error(500)), \
             caplog.at_level(logging.ERROR, logger="smbc_card_recorder.src.mailSystem"):
            result = mailSystem.initialize_services()

        assert result is False
        assert "Gmail APIサービスへの接続中にエラーが発生しました" in caplog.text

    def test_returns_false_when_service_account_env_missing(self, monkeypatch, tmp_path, caplog):
        (tmp_path / "gmailtoken.json").write_text("{}", encoding="utf-8")
        monkeypatch.setattr(mailSystem, "SERVICE_ACCOUNT_FILE", None)
        mock_creds = MagicMock(valid=True)

        with patch("src.mailSystem.GmailCredentials.from_authorized_user_file", return_value=mock_creds), \
             patch("src.mailSystem.build", return_value=MagicMock()), \
             caplog.at_level(logging.ERROR, logger="smbc_card_recorder.src.mailSystem"):
            result = mailSystem.initialize_services()

        assert result is False
        assert "SERVICE_ACCOUNT_FILE" in caplog.text

    def test_returns_false_when_service_account_file_not_found(self, tmp_path, caplog):
        (tmp_path / "gmailtoken.json").write_text("{}", encoding="utf-8")
        mock_creds = MagicMock(valid=True)

        with patch("src.mailSystem.GmailCredentials.from_authorized_user_file", return_value=mock_creds), \
             patch("src.mailSystem.build", return_value=MagicMock()), \
             patch("src.mailSystem.gspread.service_account", side_effect=FileNotFoundError), \
             caplog.at_level(logging.ERROR, logger="smbc_card_recorder.src.mailSystem"):
            result = mailSystem.initialize_services()

        assert result is False
        assert "サービスアカウントファイル" in caplog.text

    def test_returns_false_when_gspread_auth_raises_generic_exception(self, tmp_path, caplog):
        (tmp_path / "gmailtoken.json").write_text("{}", encoding="utf-8")
        mock_creds = MagicMock(valid=True)

        with patch("src.mailSystem.GmailCredentials.from_authorized_user_file", return_value=mock_creds), \
             patch("src.mailSystem.build", return_value=MagicMock()), \
             patch("src.mailSystem.gspread.service_account", side_effect=ValueError("bad key")), \
             caplog.at_level(logging.ERROR, logger="smbc_card_recorder.src.mailSystem"):
            result = mailSystem.initialize_services()

        assert result is False
        assert "Google スプレッドシートAPIの認証エラー" in caplog.text


class TestCheckMailJob:
    def test_returns_early_when_services_not_initialized(self, caplog):
        mailSystem.gmail_service = None
        mailSystem.gc = MagicMock()
        with caplog.at_level(logging.ERROR, logger="smbc_card_recorder.src.mailSystem"):
            mailSystem.check_mail_job()
        assert "サービスが初期化されていません" in caplog.text

        caplog.clear()
        mailSystem.gmail_service = MagicMock()
        mailSystem.gc = None
        with caplog.at_level(logging.ERROR, logger="smbc_card_recorder.src.mailSystem"):
            mailSystem.check_mail_job()
        assert "サービスが初期化されていません" in caplog.text

    def test_first_run_query_without_after_and_no_messages_saves_state(self, tmp_path):
        gmail = MagicMock()
        gmail.users().messages().list().execute.return_value = {}
        mailSystem.gmail_service = gmail
        mailSystem.gc = MagicMock()

        mailSystem.check_mail_job()

        list_call = gmail.users().messages().list.call_args
        assert list_call.kwargs == {
            "userId": "me",
            "q": 'in:inbox subject:"ご利用のお知らせ【三井住友カード】"',
        }
        assert (tmp_path / "last_run_time.json").exists()
        assert (tmp_path / "processed_message_ids.json").exists()

    def test_subsequent_run_appends_after_timestamp_to_query(self):
        last_time = datetime.datetime(2026, 11, 20, 12, 0, 0)
        mailSystem.save_current_run_time(last_time)

        gmail = MagicMock()
        gmail.users().messages().list().execute.return_value = {"messages": []}
        mailSystem.gmail_service = gmail
        mailSystem.gc = MagicMock()

        mailSystem.check_mail_job()

        expected_ts = int(last_time.timestamp())
        list_call = gmail.users().messages().list.call_args
        assert list_call.kwargs["q"] == (
            f'in:inbox subject:"ご利用のお知らせ【三井住友カード】" after:{expected_ts}'
        )

    def test_processes_new_messages_and_skips_already_processed(self, tmp_path):
        mailSystem.processed_ids = {"already_done"}

        gmail = MagicMock()
        gmail.users().messages().list().execute.return_value = {
            "messages": [{"id": "already_done"}, {"id": "new_1"}, {"id": "new_2"}]
        }

        def _get_side_effect(userId, id, format):
            req = MagicMock()
            if id == "new_1":
                req.execute.return_value = {
                    "payload": {
                        "headers": [
                            {"name": "From", "value": "statement@vpass.ne.jp"},
                            {"name": "Date", "value": "Fri, 27 Nov 2026 10:00:00 +0900"},
                            {"name": "Subject", "value": "ご利用のお知らせ【三井住友カード】"},
                        ],
                        "body": {"data": encode_b64url("本文1")},
                    }
                }
            elif id == "new_2":
                req.execute.return_value = {
                    "payload": {
                        "headers": [],  # 件名ヘッダーなし -> "件名なし"
                        "body": {"data": encode_b64url("本文2")},
                    }
                }
            return req

        gmail.users().messages().get.side_effect = _get_side_effect
        mailSystem.gmail_service = gmail
        mailSystem.gc = MagicMock()

        with patch.object(mailSystem, "system") as mock_system:
            mailSystem.check_mail_job()

        # already_done に対しては get() が呼ばれない
        called_ids = [c.kwargs["id"] for c in gmail.users().messages().get.call_args_list]
        assert called_ids == ["new_1", "new_2"]
        assert [c.args[0] for c in mock_system.call_args_list] == ["本文1", "本文2"]
        assert mailSystem.processed_ids == {"already_done", "new_1", "new_2"}

        saved_ids = set(json.loads((tmp_path / "processed_message_ids.json").read_text(encoding="utf-8")))
        assert saved_ids == {"already_done", "new_1", "new_2"}

    def test_empty_body_skips_system_but_marks_processed(self):
        gmail = MagicMock()
        gmail.users().messages().list().execute.return_value = {"messages": [{"id": "empty_msg"}]}
        gmail.users().messages().get().execute.return_value = {
            "payload": {"headers": [{"name": "subject", "value": "テスト"}], "parts": []}
        }
        mailSystem.gmail_service = gmail
        mailSystem.gc = MagicMock()

        with patch.object(mailSystem, "system") as mock_system:
            mailSystem.check_mail_job()

        mock_system.assert_not_called()
        assert "empty_msg" in mailSystem.processed_ids

    def test_per_message_http_error_and_generic_error_do_not_block_other_messages(self):
        gmail = MagicMock()
        gmail.users().messages().list().execute.return_value = {
            "messages": [{"id": "err_http"}, {"id": "err_proc"}, {"id": "ok_msg"}]
        }

        def _get_side_effect(userId, id, format):
            req = MagicMock()
            if id == "err_http":
                req.execute.side_effect = _make_http_error(503)
            elif id == "err_proc":
                req.execute.return_value = {
                    "payload": {
                        "headers": [],
                        "body": {"data": encode_b64url("壊れる本文")},
                    }
                }
            else:
                req.execute.return_value = {
                    "payload": {
                        "headers": [{"name": "Subject", "value": "正常"}],
                        "body": {"data": encode_b64url("正常本文")},
                    }
                }
            return req

        gmail.users().messages().get.side_effect = _get_side_effect
        mailSystem.gmail_service = gmail
        mailSystem.gc = MagicMock()

        def _system_side_effect(body):
            if body == "壊れる本文":
                raise RuntimeError("parse crash")

        with patch.object(mailSystem, "system", side_effect=_system_side_effect):
            mailSystem.check_mail_job()

        assert "err_http" not in mailSystem.processed_ids
        assert "err_proc" not in mailSystem.processed_ids
        assert "ok_msg" in mailSystem.processed_ids

    def test_list_http_error_notifies_discord_and_does_not_save_state(self, tmp_path):
        gmail = MagicMock()
        gmail.users().messages().list().execute.side_effect = _make_http_error(403)
        mailSystem.gmail_service = gmail
        mailSystem.gc = MagicMock()

        with patch("src.mailSystem.mailDisco.MailDisco") as mock_disco:
            mailSystem.check_mail_job()

        mock_disco.assert_called_once_with("❌ Gmail API呼び出し中にエラーが発生しました")
        mock_disco.return_value.send.assert_called_once()
        assert not (tmp_path / "last_run_time.json").exists()

    def test_list_unexpected_error_notifies_discord_and_does_not_save_state(self, tmp_path):
        gmail = MagicMock()
        gmail.users().messages().list().execute.side_effect = RuntimeError("network down")
        mailSystem.gmail_service = gmail
        mailSystem.gc = MagicMock()

        with patch("src.mailSystem.mailDisco.MailDisco") as mock_disco:
            mailSystem.check_mail_job()

        mock_disco.assert_called_once_with("ジョブ実行で予期せぬエラーが発生しました")
        mock_disco.return_value.send.assert_called_once()
        assert not (tmp_path / "last_run_time.json").exists()


class TestEndToEndFlow:
    def test_full_pipeline_from_gmail_to_sheet_discord_and_dedup_on_second_run(self, monkeypatch, tmp_path):
        """
        Gmail受信 -> Base64デコード -> 正規表現抽出 -> Purposeカテゴリ判定
        -> Google Sheets書き込み -> Discord通知 -> 状態ファイル保存 -> 2回目実行での重複スキップ
        を一気通貫で検証する。
        """
        # Purpose 用の categories.csv を tmp_path に配置
        fake_src = tmp_path / "src"
        fake_src.mkdir()
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "categories.csv").write_text(
            "スーパーA,コンビニC\nレストランA\nドラッグストアA\nJR東日本\nAmazon\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(purpose_module, "__file__", str(fake_src / "purpose.py"))
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/e2e/token")

        # Gmail モック
        email_text = (
            "ご利用日時：2026/11/27 18:45\n"
            "ご利用場所：スーパーA\n"
            "ご利用金額：2,580円\n"
        )
        gmail = MagicMock()
        gmail.users().messages().list().execute.return_value = {"messages": [{"id": "e2e_msg_1"}]}
        gmail.users().messages().get().execute.return_value = {
            "payload": {
                "headers": [{"name": "Subject", "value": "ご利用のお知らせ【三井住友カード】"}],
                "parts": [
                    {"mimeType": "text/plain", "body": {"data": encode_b64url(email_text, strip_padding=True)}}
                ],
            }
        }

        # GSpread モック
        ws = MagicMock()
        ws.get_all_values.return_value = [
            ["日時", "場所", "金額", "用途"],
            ["", "", "", ""],
            ["", "", "", ""],
        ]
        spreadsheet = MagicMock()
        spreadsheet.worksheet.return_value = ws
        gc = MagicMock()
        gc.open_by_key.return_value = spreadsheet

        mailSystem.gmail_service = gmail
        mailSystem.gc = gc

        with patch("src.mailDisco.requests.post", return_value=MagicMock(ok=True, status_code=204)) as mock_post:
            # 1回目: 正常処理される
            mailSystem.check_mail_job()

            spreadsheet.worksheet.assert_called_once_with("11月")
            ws.update.assert_called_once_with(
                values=[["2026/11/27 18:45", "スーパーA", 2580, "食費"]],
                range_name="A3:D3",
            )
            mock_post.assert_called_once_with(
                "https://discord.com/api/webhooks/e2e/token",
                json={"content": "【正常に書き込みました】\n利用先: スーパーA"},
                timeout=5,
            )

            # 2回目: 同じメールが返ってきても処理済みIDによりスキップされる
            mailSystem.check_mail_job()
            assert ws.update.call_count == 1
            assert mock_post.call_count == 1


class TestMainEntrypoint:
    def test_main_block_success_registers_schedule_and_handles_keyboard_interrupt(self):
        import io
        import os
        import runpy
        import schedule as schedule_mod

        real_open = open
        real_exists = os.path.exists

        def _safe_open(file, mode="r", *args, **kwargs):
            if "w" in mode:
                return io.StringIO()
            return real_open(file, mode, *args, **kwargs)

        def _safe_exists(path):
            if str(path).endswith((".json",)):
                return False
            return real_exists(path)

        schedule_mod.clear()
        mock_disco_instance = MagicMock()
        mock_gmail = MagicMock()
        mock_gmail.users().messages().list().execute.return_value = {"messages": []}
        try:
            with patch("dotenv.load_dotenv"), \
                 patch("src.mailDisco.MailDisco", return_value=mock_disco_instance) as mock_disco_cls, \
                 patch("google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file") as mock_flow_cls, \
                 patch("googleapiclient.discovery.build", return_value=mock_gmail), \
                 patch("gspread.service_account", return_value=MagicMock()), \
                 patch("os.path.exists", side_effect=_safe_exists), \
                 patch("builtins.open", side_effect=_safe_open), \
                 patch("os.getenv", side_effect=lambda k, d=None: "dummy" if k in ("SERVICE_ACCOUNT_FILE", "SPREADSHEET_ID") else d), \
                 patch("time.sleep", side_effect=[None, KeyboardInterrupt]):
                mock_creds = MagicMock(valid=True)
                mock_creds.to_json.return_value = "{}"
                mock_flow_cls.return_value.run_local_server.return_value = mock_creds

                runpy.run_path(mailSystem.__file__, run_name="__main__")

            # :00 と :30 の2つのジョブが登録されていること
            assert len(schedule_mod.jobs) == 2
            at_times = sorted(str(j.at_time) for j in schedule_mod.jobs)
            assert at_times == ["00:00:00", "00:30:00"]

            messages = [c.args[0] for c in mock_disco_cls.call_args_list]
            assert any("起動しました" in m for m in messages)
            assert any("停止しました" in m for m in messages)
        finally:
            schedule_mod.clear()

    def test_main_block_failure_notifies_and_exits_without_scheduling(self):
        import os
        import runpy
        import schedule as schedule_mod

        real_exists = os.path.exists

        def _safe_exists(path):
            if str(path).endswith((".json",)):
                return False
            return real_exists(path)

        schedule_mod.clear()
        mock_disco_instance = MagicMock()
        try:
            with patch("dotenv.load_dotenv"), \
                 patch("src.mailDisco.MailDisco", return_value=mock_disco_instance) as mock_disco_cls, \
                 patch("os.path.exists", side_effect=_safe_exists), \
                 patch("google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file", side_effect=FileNotFoundError):
                runpy.run_path(mailSystem.__file__, run_name="__main__")

            assert len(schedule_mod.jobs) == 0
            messages = [c.args[0] for c in mock_disco_cls.call_args_list]
            assert any("サービスの初期化に失敗しました" in m for m in messages)
        finally:
            schedule_mod.clear()
