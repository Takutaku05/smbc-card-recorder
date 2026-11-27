"""
PR #58 レビュー指摘への対応の回帰テスト。

* gspread 再試行の上限化（BoundedBackOffHTTPClient）
* Gmail 検索から送信済み・下書きを除外し、検索範囲を処理済みIDの保持期間内に限定
* 恒久的に失敗するメールの再試行上限（失敗回数の記録・打ち切り・通知抑止）
* SIGTERM 受信時の安全な停止（1通単位の区切りで中断）
* 整形済み HTML メールの改行正規化 / 補助 text/* パートのスキップ / body=None
* Discord webhook_url="" 明示指定 / 停止通知の再試行なし
* chmod 失敗時も起動を継続
"""

import datetime
import json
import logging
import os
from unittest.mock import MagicMock, patch

import gspread
import pytest
import requests

import src.logger_config as logger_config
import src.mailDisco as mailDisco
import src.mailSystem as mailSystem
from tests.conftest import encode_b64url, expected_earliest_after

LOGGER = "smbc_card_recorder.src.mailSystem"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _api_error(code):
    response = MagicMock()
    response.json.return_value = {
        "error": {"code": code, "message": "err", "status": "X"}
    }
    return gspread.exceptions.APIError(response)


def _gmail(message_ids, details=None):
    """messages.list が message_ids を返し、messages.get が details[id] を返すモック。"""
    details = details or {}
    gmail = MagicMock()
    gmail.users().messages().list().execute.return_value = {
        "messages": [{"id": mid} for mid in message_ids]
    }

    def _get(userId, id, format):
        req = MagicMock()
        detail = details.get(
            id, {"payload": {"headers": [{"name": "Subject", "value": "件名"}]}}
        )
        if isinstance(detail, Exception):
            req.execute.side_effect = detail
        else:
            req.execute.return_value = detail
        return req

    gmail.users().messages().get.side_effect = _get
    return gmail


# ---------------------------------------------------------------------------
# BoundedBackOffHTTPClient
# ---------------------------------------------------------------------------
class TestBoundedBackOffHTTPClient:
    def _client(self):
        return mailSystem.BoundedBackOffHTTPClient(
            auth=MagicMock(), session=MagicMock()
        )

    def test_retries_rate_limit_even_for_post_then_succeeds(self):
        ok = MagicMock()
        with (
            patch.object(
                gspread.http_client.HTTPClient,
                "request",
                side_effect=[_api_error(429), _api_error(429), ok],
            ) as base_request,
            patch.object(mailSystem.time, "sleep") as sleep,
        ):
            assert (
                self._client().request("post", "https://example.invalid/append") is ok
            )
        assert base_request.call_count == 3
        assert [c.args[0] for c in sleep.call_args_list] == [2, 4]

    def test_retries_server_error_for_idempotent_get(self):
        ok = MagicMock()
        with (
            patch.object(
                gspread.http_client.HTTPClient,
                "request",
                side_effect=[_api_error(503), _api_error(408), ok],
            ),
            patch.object(mailSystem.time, "sleep"),
        ):
            assert self._client().request("GET", "https://example.invalid/values") is ok

    def test_does_not_retry_server_error_for_post_to_avoid_duplicate_append(self):
        with (
            patch.object(
                gspread.http_client.HTTPClient, "request", side_effect=_api_error(503)
            ) as base_request,
            patch.object(mailSystem.time, "sleep") as sleep,
            pytest.raises(gspread.exceptions.APIError),
        ):
            self._client().request("post", "https://example.invalid/append")
        assert base_request.call_count == 1
        sleep.assert_not_called()

    def test_does_not_retry_client_error(self):
        with (
            patch.object(
                gspread.http_client.HTTPClient, "request", side_effect=_api_error(403)
            ) as base_request,
            patch.object(mailSystem.time, "sleep") as sleep,
            pytest.raises(gspread.exceptions.APIError),
        ):
            self._client().request("get", "https://example.invalid/values")
        assert base_request.call_count == 1
        sleep.assert_not_called()

    def test_gives_up_after_max_retries_with_capped_wait(self):
        with (
            patch.object(
                gspread.http_client.HTTPClient, "request", side_effect=_api_error(503)
            ) as base_request,
            patch.object(mailSystem.time, "sleep") as sleep,
            pytest.raises(gspread.exceptions.APIError),
        ):
            self._client().request("get", "https://example.invalid/values")
        max_retries = mailSystem.BoundedBackOffHTTPClient.MAX_RETRIES
        assert base_request.call_count == max_retries + 1
        assert [c.args[0] for c in sleep.call_args_list] == [2, 4, 8, 16, 32]


# ---------------------------------------------------------------------------
# Gmail 検索クエリ
# ---------------------------------------------------------------------------
class TestGmailQuery:
    def test_excludes_sent_and_drafts_to_avoid_recording_forwarded_mail(
        self, pinned_job_now
    ):
        gmail = _gmail([])
        mailSystem.gmail_service = gmail
        mailSystem.gc = MagicMock()
        mailSystem.check_mail_job()
        q = gmail.users().messages().list.call_args.kwargs["q"]
        assert "-in:sent" in q
        assert "-in:drafts" in q

    def test_stale_last_run_time_is_clamped_to_search_window(
        self, caplog, pinned_job_now
    ):
        stale = pinned_job_now - datetime.timedelta(
            days=mailSystem.SEARCH_WINDOW_DAYS + 30
        )
        mailSystem.save_current_run_time(stale)
        gmail = _gmail([])
        mailSystem.gmail_service = gmail
        mailSystem.gc = MagicMock()
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            mailSystem.check_mail_job()
        q = gmail.users().messages().list.call_args.kwargs["q"]
        assert q.endswith(f"after:{expected_earliest_after(pinned_job_now)}")
        assert "検索範囲の上限" in caplog.text

    def test_search_window_is_shorter_than_processed_id_retention(self):
        # 検索範囲内のメールの処理済みIDがパージ済みにならないことの前提
        assert mailSystem.SEARCH_WINDOW_DAYS < mailSystem.PROCESSED_IDS_RETENTION_DAYS


# ---------------------------------------------------------------------------
# 恒久的に失敗するメールの再試行上限
# ---------------------------------------------------------------------------
class TestMessageFailureCap:
    def _run_empty_body_job(self):
        mailSystem.gmail_service = _gmail(
            ["bad"],
            {
                "bad": {
                    "payload": {
                        "headers": [{"name": "Subject", "value": "S"}],
                        "parts": [],
                    }
                }
            },
        )
        mailSystem.gc = MagicMock()
        with patch("src.mailSystem.mailDisco.MailDisco") as disco:
            mailSystem.check_mail_job()
        return [c.args[0] for c in disco.call_args_list]

    def test_retries_until_cap_then_marks_processed_and_stops_notifying(self, tmp_path):
        last_run_file = tmp_path / "last_run_time.json"
        all_messages = []
        for attempt in range(1, mailSystem.MAX_MESSAGE_ATTEMPTS):
            all_messages.append(self._run_empty_body_job())
            assert "bad" not in mailSystem.processed_ids
            assert mailSystem._message_failure_counts == {"bad": attempt}
            assert not last_run_file.exists()

        # 初回の失敗だけ通知し、2回目以降は通知しない
        assert len(all_messages[0]) == 1
        assert "本文を取得できませんでした" in all_messages[0][0]
        assert all(msgs == [] for msgs in all_messages[1:])

        final_messages = self._run_empty_body_job()
        assert "bad" in mailSystem.processed_ids
        assert mailSystem._message_failure_counts == {}
        assert len(final_messages) == 1
        assert (
            f"{mailSystem.MAX_MESSAGE_ATTEMPTS} 回連続で処理に失敗" in final_messages[0]
        )
        # 打ち切ったメールで last_run_time が凍結されない
        assert last_run_file.exists()
        saved_ids = json.loads(
            (tmp_path / "processed_message_ids.json").read_text(encoding="utf-8")
        )
        assert "bad" in saved_ids
        saved_counts = json.loads(
            (tmp_path / "failed_message_attempts.json").read_text(encoding="utf-8")
        )
        assert saved_counts == {}

        # 打ち切り後は再取得もされない
        assert self._run_empty_body_job() == []

    def test_success_after_failure_clears_failure_count(self, tmp_path):
        mailSystem._message_failure_counts = {"m1": 3}
        mailSystem.gmail_service = _gmail(["m1"])
        mailSystem.gc = MagicMock()
        with (
            patch.object(mailSystem, "get_email_body", return_value="body"),
            patch.object(mailSystem, "system", return_value=True),
        ):
            mailSystem.check_mail_job()
        assert mailSystem._message_failure_counts == {}
        assert (
            json.loads(
                (tmp_path / "failed_message_attempts.json").read_text(encoding="utf-8")
            )
            == {}
        )

    def test_http_error_on_get_is_counted_with_subject_placeholder(self):
        from googleapiclient.errors import HttpError

        error = HttpError(MagicMock(status=503, reason="unavailable"), b"{}")
        mailSystem.gmail_service = _gmail(["h1"], {"h1": error})
        mailSystem.gc = MagicMock()
        with patch("src.mailSystem.mailDisco.MailDisco") as disco:
            mailSystem.check_mail_job()
        assert mailSystem._message_failure_counts == {"h1": 1}
        assert "件名: 取得前" in disco.call_args.args[0]

    def test_load_failure_counts_from_file(self, tmp_path):
        (tmp_path / "failed_message_attempts.json").write_text(
            json.dumps({"a": 2, "b": "3"}), encoding="utf-8"
        )
        mailSystem.load_message_failure_counts()
        assert mailSystem._message_failure_counts == {"a": 2, "b": 3}

    def test_load_failure_counts_missing_file_starts_empty(self):
        mailSystem._message_failure_counts = {"stale": 1}
        mailSystem.load_message_failure_counts()
        assert mailSystem._message_failure_counts == {}

    @pytest.mark.parametrize("content", ["{broken", "[1, 2]", '{"a": "x"}'])
    def test_load_failure_counts_corrupt_file_resets(self, tmp_path, caplog, content):
        (tmp_path / "failed_message_attempts.json").write_text(
            content, encoding="utf-8"
        )
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            mailSystem.load_message_failure_counts()
        assert mailSystem._message_failure_counts == {}
        assert "処理失敗回数ファイル" in caplog.text

    def test_save_failure_counts_logs_os_error(self, caplog):
        with (
            patch.object(
                mailSystem, "_atomic_write_json", side_effect=OSError("disk full")
            ),
            caplog.at_level(logging.ERROR, logger=LOGGER),
        ):
            mailSystem.save_message_failure_counts()
        assert "処理失敗回数の保存中にエラー" in caplog.text


# ---------------------------------------------------------------------------
# SIGTERM 受信時の中断
# ---------------------------------------------------------------------------
def test_shutdown_request_stops_between_messages_without_advancing_last_run(tmp_path):
    mailSystem.gmail_service = _gmail(
        ["second", "first"]
    )  # 降順で返る → first から処理
    mailSystem.gc = MagicMock()
    processed_bodies = []

    def _system(body):
        processed_bodies.append(body)
        mailSystem._shutdown_requested = True  # 1通目の処理中に SIGTERM を受信
        return True

    with (
        patch.object(mailSystem, "get_email_body", return_value="body"),
        patch.object(mailSystem, "system", side_effect=_system),
    ):
        mailSystem.check_mail_job()

    assert processed_bodies == ["body"]
    assert mailSystem.processed_ids == {"first"}
    # 1通目の処理済み登録は保存済み、last_run_time は進めず次回に残りを再開する
    assert "first" in json.loads(
        (tmp_path / "processed_message_ids.json").read_text(encoding="utf-8")
    )
    assert not (tmp_path / "last_run_time.json").exists()


# ---------------------------------------------------------------------------
# 本文抽出
# ---------------------------------------------------------------------------
class TestBodyExtraction:
    def test_formatted_html_table_keeps_label_and_value_on_same_line(self):
        html_body = (
            "<html><body>\n<table>\n"
            "  <tr>\n    <td>ご利用日時：</td>\n    <td>2026/11/20 12:34</td>\n  </tr>\n"
            "  <tr>\n    <td>ご利用場所：</td>\n    <td>スーパーA</td>\n  </tr>\n"
            "  <tr>\n    <td>ご利用金額：</td>\n    <td>1,234円</td>\n  </tr>\n"
            "</table>\n</body></html>"
        )
        payload = {"mimeType": "text/html", "body": {"data": encode_b64url(html_body)}}
        parsed = mailSystem.parse_mail_body(mailSystem.get_email_body(payload))
        assert parsed == {
            "datetime": "2026/11/20 12:34",
            "location": "スーパーA",
            "amount": 1234,
        }

    def test_auxiliary_text_parts_are_not_taken_as_body(self):
        payload = {
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "mimeType": "text/calendar",
                    "body": {"data": encode_b64url("BEGIN:VCALENDAR")},
                },
                {"mimeType": "text/csv", "body": {"data": encode_b64url("a,b,c")}},
                {"mimeType": "text/plain", "body": {"data": encode_b64url("本文です")}},
            ],
        }
        assert mailSystem.get_email_body(payload) == "本文です"

    def test_part_with_body_none_does_not_raise(self):
        payload = {
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/plain", "body": None},
                {
                    "mimeType": "text/html",
                    "body": {"data": encode_b64url("<p>HTML本文</p>")},
                },
            ],
        }
        assert mailSystem.get_email_body(payload).strip() == "HTML本文"


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------
class TestDiscord:
    def test_explicit_empty_webhook_url_disables_without_env_fallback(
        self, monkeypatch
    ):
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://example.invalid/hook")
        session = MagicMock()
        assert (
            mailDisco.DiscordNotifier(webhook_url="", session=session).send("msg")
            is False
        )
        session.post.assert_not_called()

    def test_mentions_in_mail_derived_text_are_not_parsed(self):
        session = MagicMock()
        session.post.return_value = MagicMock(ok=True)
        mailDisco.DiscordNotifier(
            webhook_url="https://example.invalid/hook", session=session
        ).send("件名: @everyone 至急")
        payload = session.post.call_args.kwargs["json"]
        assert payload["allowed_mentions"] == {"parse": []}

    def test_mail_disco_max_retries_zero_does_not_wait_on_rate_limit(self):
        session = MagicMock()
        session.post.return_value = MagicMock(ok=False, status_code=429)
        with patch.object(mailDisco.time, "sleep") as sleep:
            sent = mailDisco.MailDisco(
                "stop",
                webhook_url="https://example.invalid/hook",
                session=session,
                max_retries=0,
            ).send()
        assert sent is False
        assert session.post.call_count == 1
        sleep.assert_not_called()


# ---------------------------------------------------------------------------
# 権限変更の失敗で起動を止めない
# ---------------------------------------------------------------------------
class TestPermissionErrorsDoNotCrash:
    def test_logger_chmod_failure_warns_to_stderr(self, tmp_path, capsys):
        target = tmp_path / "x.log"
        target.write_text("", encoding="utf-8")
        with patch.object(
            logger_config.os, "chmod", side_effect=PermissionError("not owner")
        ):
            logger_config._chmod_best_effort(str(target), 0o600)
        assert "権限を 0o600 に変更できませんでした" in capsys.readouterr().err

    def test_token_dir_chmod_failure_still_writes_token(self, tmp_path, caplog):
        token_path = tmp_path / "config" / "gmailtoken.json"
        with (
            patch.object(
                mailSystem.os, "chmod", side_effect=PermissionError("not owner")
            ),
            caplog.at_level(logging.WARNING, logger=LOGGER),
        ):
            mailSystem._write_token_atomic(str(token_path), '{"t": 1}')
        assert token_path.read_text(encoding="utf-8") == '{"t": 1}'
        assert oct(os.stat(token_path).st_mode & 0o777) == "0o600"
        assert "権限を 0700 に変更できませんでした" in caplog.text

    def test_initial_token_save_failure_does_not_abort_initialization(self, caplog):
        creds = MagicMock()
        creds.to_json.return_value = '{"new": true}'
        flow = MagicMock()
        flow.run_local_server.return_value = creds
        with (
            patch(
                "src.mailSystem.InstalledAppFlow.from_client_secrets_file",
                return_value=flow,
            ),
            patch.object(
                mailSystem,
                "_write_token_atomic",
                side_effect=PermissionError("not owner"),
            ),
            patch("src.mailSystem.build"),
            patch("src.mailSystem.gspread.service_account"),
            caplog.at_level(logging.ERROR, logger=LOGGER),
        ):
            assert mailSystem.initialize_services() is True
        assert "Gmailトークンの保存に失敗しました" in caplog.text


# ---------------------------------------------------------------------------
# 2回目のレビュー指摘への対応
# ---------------------------------------------------------------------------
class TestSecondReviewFixes:
    def test_non_json_5xx_is_retried_using_http_status(self):
        """HTML の 503 など JSON を解析できない応答では err.code が -1 になるため HTTP ステータスで判定する。"""
        response = MagicMock(status_code=503, text="<html>Service Unavailable</html>")
        response.json.side_effect = ValueError("not json")
        html_error = gspread.exceptions.APIError(response)
        assert html_error.code == -1
        ok = MagicMock()
        with (
            patch.object(
                gspread.http_client.HTTPClient, "request", side_effect=[html_error, ok]
            ),
            patch.object(mailSystem.time, "sleep") as sleep,
        ):
            client = mailSystem.BoundedBackOffHTTPClient(
                auth=MagicMock(), session=MagicMock()
            )
            assert client.request("get", "https://example.invalid/values") is ok
        sleep.assert_called_once_with(2)

    def test_br_with_attributes_and_head_are_handled(self):
        html_body = (
            "<html><head><title>ご利用金額のお知らせ 9,999円</title></head><body>"
            '<p>ご利用日時：2026/11/20 12:34<br class="sp">ご利用場所：スーパーA'
            '<BR style="line-height:1">ご利用金額：1,000円</p></body></html>'
        )
        payload = {"mimeType": "text/html", "body": {"data": encode_b64url(html_body)}}
        parsed = mailSystem.parse_mail_body(mailSystem.get_email_body(payload))
        assert parsed == {
            "datetime": "2026/11/20 12:34",
            "location": "スーパーA",
            "amount": 1000,
        }

    @pytest.mark.parametrize(
        "raw",
        [
            "2026/13/01 10:00",
            "2026/02/30 10:00",
            "2026/11/20 25:00",
            "2026/11/20 10:00:61",
        ],
    )
    def test_calendar_invalid_datetime_is_treated_as_na(self, raw):
        parsed = mailSystem.parse_mail_body(
            f"ご利用日時：{raw}\nご利用場所：店\nご利用金額：100円"
        )
        assert parsed["datetime"] == "N/A"

    def test_sheet_failure_is_notified_only_on_first_attempt(self):
        """Sheets 障害中に同じメールの書き込み失敗が30分ごとに通知され続けない。"""
        gc = MagicMock()
        gc.open_by_key.side_effect = RuntimeError("503")
        mailSystem.gc = gc
        sent = []
        body = (
            "ご利用日時：2026/11/20 12:34\nご利用場所：スーパーA\nご利用金額：1,000円"
        )
        with (
            patch.object(mailSystem, "get_email_body", return_value=body),
            patch("src.mailSystem.mailDisco.MailDisco") as disco,
        ):
            for _ in range(mailSystem.MAX_MESSAGE_ATTEMPTS):
                mailSystem.gmail_service = _gmail(["w1"])
                disco.reset_mock()
                mailSystem.check_mail_job()
                sent.append([c.args[0] for c in disco.call_args_list])

        # 1回目: sheet() の詳細通知（利用先・金額・エラー）のみ
        assert len(sent[0]) == 1
        assert "スプレッドシートへの書き込みに失敗しました" in sent[0][0]
        assert "スーパーA" in sent[0][0]
        # 2〜5回目: 通知なし
        assert all(msgs == [] for msgs in sent[1:-1])
        # 6回目: 打ち切り通知のみ
        assert len(sent[-1]) == 1
        assert "回連続で処理に失敗" in sent[-1][0]
        # 通知抑止フラグは処理後に元へ戻る
        assert mailSystem._sheet_failure_notify_enabled is True

    def test_failure_count_is_persisted_even_if_later_message_raises_refresh_error(
        self, tmp_path
    ):
        from google.auth.exceptions import RefreshError

        mailSystem.gmail_service = _gmail(
            ["second", "first"],
            {
                "first": {"payload": {"headers": [], "parts": []}},  # 本文なし → 失敗
                "second": RefreshError("token revoked"),
            },
        )
        mailSystem.gc = MagicMock()
        with patch("src.mailSystem.mailDisco.MailDisco"):
            mailSystem.check_mail_job()
        saved = json.loads(
            (tmp_path / "failed_message_attempts.json").read_text(encoding="utf-8")
        )
        assert saved == {"first": 1}


# ---------------------------------------------------------------------------
# 3回目のレビュー指摘への対応
# ---------------------------------------------------------------------------
class TestStartupFailureClassification:
    def _expired_creds(self, tmp_path):
        (tmp_path / "gmailtoken.json").write_text("{}", encoding="utf-8")
        return MagicMock(valid=False, expired=True, refresh_token="r")

    def test_network_error_on_refresh_is_transient_and_does_not_advise_token_deletion(
        self, tmp_path, caplog
    ):
        from google.auth.exceptions import TransportError

        creds = self._expired_creds(tmp_path)
        creds.refresh.side_effect = TransportError("Name resolution failed")
        with (
            patch(
                "src.mailSystem.GmailCredentials.from_authorized_user_file",
                return_value=creds,
            ),
            patch("src.mailSystem.mailDisco.MailDisco") as disco,
            caplog.at_level(logging.ERROR, logger=LOGGER),
        ):
            assert mailSystem.initialize_services() is False
        assert mailSystem._init_failure_transient is True
        disco.assert_not_called()
        assert "削除" not in caplog.text
        # トークンには触れない
        assert (tmp_path / "gmailtoken.json").read_text(encoding="utf-8") == "{}"

    def test_refresh_error_is_permanent_and_advises_reauth(self, tmp_path):
        from google.auth.exceptions import RefreshError

        creds = self._expired_creds(tmp_path)
        creds.refresh.side_effect = RefreshError("invalid_grant")
        with (
            patch(
                "src.mailSystem.GmailCredentials.from_authorized_user_file",
                return_value=creds,
            ),
            patch("src.mailSystem.mailDisco.MailDisco") as disco,
        ):
            assert mailSystem.initialize_services() is False
        assert mailSystem._init_failure_transient is False
        assert "gmailtoken.json を削除" in disco.call_args.args[0]

    @pytest.mark.parametrize(
        ("error", "transient"),
        [
            (requests.exceptions.ConnectionError("dns"), True),
            (requests.exceptions.ReadTimeout("slow"), True),
            (_api_error(503), True),
            (_api_error(429), True),
            (_api_error(403), False),
            (ValueError("bad key file"), False),
        ],
    )
    def test_sheets_init_error_classification(self, tmp_path, error, transient):
        (tmp_path / "gmailtoken.json").write_text("{}", encoding="utf-8")
        with (
            patch(
                "src.mailSystem.GmailCredentials.from_authorized_user_file",
                return_value=MagicMock(valid=True),
            ),
            patch("src.mailSystem.build"),
            patch("src.mailSystem.gspread.service_account", side_effect=error),
        ):
            assert mailSystem.initialize_services() is False
        assert mailSystem._init_failure_transient is transient

    def test_gmail_build_http_error_classification(self, tmp_path):
        from googleapiclient.errors import HttpError

        (tmp_path / "gmailtoken.json").write_text("{}", encoding="utf-8")
        with (
            patch(
                "src.mailSystem.GmailCredentials.from_authorized_user_file",
                return_value=MagicMock(valid=True),
            ),
            patch(
                "src.mailSystem.build",
                side_effect=HttpError(MagicMock(status=503, reason="x"), b"{}"),
            ),
        ):
            assert mailSystem.initialize_services() is False
        assert mailSystem._init_failure_transient is True

    def test_is_transient_error_ignores_unrelated_exceptions(self):
        assert mailSystem._is_transient_error(RuntimeError("x")) is False

    def test_sheets_client_gets_http_timeout(self, tmp_path):
        (tmp_path / "gmailtoken.json").write_text("{}", encoding="utf-8")
        gc = MagicMock()
        with (
            patch(
                "src.mailSystem.GmailCredentials.from_authorized_user_file",
                return_value=MagicMock(valid=True),
            ),
            patch("src.mailSystem.build"),
            patch("src.mailSystem.gspread.service_account", return_value=gc),
        ):
            assert mailSystem.initialize_services() is True
        gc.set_timeout.assert_called_once_with(mailSystem.SHEETS_HTTP_TIMEOUT_SECONDS)

    def test_main_exits_with_1_without_discord_on_transient_failure(self):
        from tests.conftest import run_main_block

        result = run_main_block(
            service_account_side_effect=requests.exceptions.ConnectionError("dns")
        )
        assert result["exit_code"] == mailSystem.EXIT_TRANSIENT_FAILURE == 1
        # 再起動のたびに通知が届かないよう、一時的な失敗では Discord に送らない
        assert result["disco_messages"] == []
        result["run_pending"].assert_not_called()

    def test_sigterm_handler_is_registered_only_after_initialization(self):
        import signal

        from tests.conftest import run_main_block

        before = signal.getsignal(signal.SIGTERM)
        seen = {}

        def _capture(*_a, **_k):
            seen["handler"] = signal.getsignal(signal.SIGTERM)
            raise requests.exceptions.ConnectionError("dns")

        run_main_block(service_account_side_effect=_capture)
        # 初期化中はまだ既定のハンドラ（即終了できる）
        assert seen["handler"] is before


class TestAmbiguousAppend:
    def _gc_with_ws(self, ws):
        spreadsheet = MagicMock()
        spreadsheet.worksheet.return_value = ws
        gc = MagicMock()
        gc.open_by_key.return_value = spreadsheet
        return gc

    @pytest.mark.parametrize(
        "error",
        [_api_error(503), _api_error(408), requests.exceptions.ReadTimeout("slow")],
    )
    def test_server_error_on_append_returns_uncertain_and_notifies(self, error):
        ws = MagicMock()
        ws.get_all_values.return_value = []  # 空シート → append_row
        ws.append_row.side_effect = error
        mailSystem.gc = self._gc_with_ws(ws)
        with (
            patch("src.mailSystem.mailDisco.MailDisco") as disco,
            patch("src.mailSystem.purpose.Purpose"),
        ):
            result = mailSystem.sheet("2026/11/20 12:34", "スーパーA", 1000)
        assert result == mailSystem.SHEET_UNCERTAIN
        message = disco.call_args.args[0]
        assert "書き込まれている可能性" in message
        assert "スーパーA" in message

    def test_client_error_on_append_is_ordinary_failure(self):
        ws = MagicMock()
        ws.get_all_values.return_value = []
        ws.append_row.side_effect = _api_error(400)
        mailSystem.gc = self._gc_with_ws(ws)
        with (
            patch("src.mailSystem.mailDisco.MailDisco"),
            patch("src.mailSystem.purpose.Purpose"),
        ):
            assert mailSystem.sheet("2026/11/20 12:34", "スーパーA", 1000) is False

    def test_uncertain_result_marks_processed_without_retry(self, tmp_path):
        mailSystem.gmail_service = _gmail(["u1"])
        mailSystem.gc = MagicMock()
        with (
            patch.object(mailSystem, "get_email_body", return_value="body"),
            patch.object(mailSystem, "system", return_value=mailSystem.SHEET_UNCERTAIN),
        ):
            mailSystem.check_mail_job()
        assert "u1" in mailSystem.processed_ids
        assert mailSystem._message_failure_counts == {}
        assert (tmp_path / "last_run_time.json").exists()


class TestIso2022JpExtendedChars:
    # ① = JIS 0x2D21, ㈱ = JIS 0x2D6A（NEC 特殊文字）
    RAW = (
        b"\x1b$B"
        + b"\x2d\x21\x2d\x6a"
        + "スーパー".encode("iso-2022-jp")[3:-3]
        + b"\x1b(B"
        + b" ABC\r\n"
    )

    def test_nec_special_chars_are_decoded_via_cp932(self):
        import base64

        encoded = base64.urlsafe_b64encode(self.RAW).decode()
        assert mailSystem.decode_base64_url_safe(encoded) == "①㈱スーパー ABC\r\n"

    def test_declared_iso_2022_jp_charset_also_falls_back_to_cp932(self):
        import base64

        encoded = base64.urlsafe_b64encode(self.RAW).decode()
        assert mailSystem.decode_base64_url_safe(
            encoded, charset="iso-2022-jp"
        ).startswith("①㈱")

    def test_halfwidth_kana_escape_is_supported(self):
        # ESC ( I による JIS X 0201 片仮名（ｱ = 0x31）
        assert mailSystem._decode_iso2022jp_as_cp932(b"\x1b(I\x31\x1b(B") == "ｱ"

    @pytest.mark.parametrize(
        "raw",
        [
            b"\x1b$Z\x21\x21",  # 未知のエスケープシーケンス
            b"\x1b$B\x21",  # 2バイト目が欠けている
            b"\x1b$B\x80\x21",  # JIS の範囲外
            b"\x1b$B\x7f\x21",  # 変換後も CP932 で未定義
        ],
    )
    def test_invalid_sequences_raise_unicode_decode_error(self, raw):
        with pytest.raises(UnicodeDecodeError):
            mailSystem._decode_iso2022jp_as_cp932(raw)

    def test_esc_data_never_falls_back_to_utf8_with_raw_escape(self):
        import base64

        encoded = base64.urlsafe_b64encode(b"\x1b$Z\x21\x21").decode()
        with pytest.raises(UnicodeDecodeError):
            mailSystem.decode_base64_url_safe(encoded)


class TestLocationOnNextLine:
    def test_value_on_next_line_is_captured(self):
        parsed = mailSystem.parse_mail_body(
            "ご利用日時：2026/11/20 12:34\nご利用場所：\n　ＡＢＣストア\nご利用金額：500円"
        )
        assert parsed["location"] == "ＡＢＣストア"

    @pytest.mark.parametrize(
        "next_line", ["ご利用金額：500円", "利用金額 500円", "備考：なし"]
    )
    def test_next_line_that_is_another_label_is_not_captured(self, next_line):
        parsed = mailSystem.parse_mail_body(f"ご利用場所：\n{next_line}")
        assert parsed["location"] == "N/A"

    def test_same_line_value_takes_precedence(self):
        parsed = mailSystem.parse_mail_body("ご利用場所：スーパーA\n次の行")
        assert parsed["location"] == "スーパーA"


class TestMiscThirdReview:
    def test_processed_ids_file_with_invalid_utf8_is_reset_and_notified(self, tmp_path):
        (tmp_path / "processed_message_ids.json").write_bytes(b"\xff\xfe\x00broken")
        with patch("src.mailSystem.mailDisco.MailDisco") as disco:
            mailSystem.load_processed_ids()
        assert mailSystem.processed_ids == set()
        disco.assert_called_once()

    def test_stale_token_tmp_with_loose_permission_is_replaced_by_0600(self, tmp_path):
        token_path = tmp_path / "gmailtoken.json"
        stale = tmp_path / "gmailtoken.json.tmp"
        stale.write_text("old", encoding="utf-8")
        os.chmod(stale, 0o644)
        mailSystem._write_token_atomic(str(token_path), '{"t": 2}')
        assert oct(os.stat(token_path).st_mode & 0o777) == "0o600"
        assert token_path.read_text(encoding="utf-8") == '{"t": 2}'
        assert not stale.exists()

    def test_orphan_failure_counts_are_pruned_when_mail_disappears(self, tmp_path):
        mailSystem._message_failure_counts = {"trashed": 2, "still_here": 1}
        mailSystem.gmail_service = _gmail(["still_here"])
        mailSystem.gc = MagicMock()
        with (
            patch.object(mailSystem, "get_email_body", return_value=""),
            patch("src.mailSystem.mailDisco.MailDisco"),
        ):
            mailSystem.check_mail_job()
        assert mailSystem._message_failure_counts == {"still_here": 2}
        assert json.loads(
            (tmp_path / "failed_message_attempts.json").read_text(encoding="utf-8")
        ) == {"still_here": 2}

    def test_migrating_large_legacy_id_list_keeps_all_ids(self, tmp_path):
        legacy = [f"id{i}" for i in range(2500)]
        (tmp_path / "processed_message_ids.json").write_text(
            json.dumps(legacy), encoding="utf-8"
        )
        mailSystem.load_processed_ids()
        mailSystem.save_processed_ids()
        assert mailSystem.processed_ids == set(legacy)


@pytest.mark.parametrize("text", ["漢字", "腕輪", "龠", "スーパー", "ご利用金額：1,000円"])
def test_iso2022jp_cp932_decoder_matches_standard_codec_for_jis_x_0208(text):
    """偶数区・奇数区・0x5F区以降のいずれも標準の iso-2022-jp コーデックと同じ結果になる。"""
    raw = text.encode("iso-2022-jp")
    assert mailSystem._decode_iso2022jp_as_cp932(raw) == raw.decode("iso-2022-jp")


def test_iso2022jp_cp932_decoder_rejects_chars_undefined_in_cp932():
    # JIS 9区（0x29xx）は Shift_JIS 0x85xx に対応し、CP932 では未定義
    with pytest.raises(UnicodeDecodeError, match="cp932 conversion failed"):
        mailSystem._decode_iso2022jp_as_cp932(b"\x1b$B\x29\x21\x1b(B")
