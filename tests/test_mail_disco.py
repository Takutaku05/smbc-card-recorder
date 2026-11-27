import logging
from unittest.mock import MagicMock, patch

import pytest
import requests

import src.mailDisco as mailDisco
from src.mailDisco import DiscordNotifier, MailDisco


class TestMailDisco:
    def test_init_stores_message(self):
        disco = MailDisco("テスト通知")
        assert disco.message == "テスト通知"

    @pytest.mark.parametrize("status_code", [200, 204])
    def test_send_success(self, monkeypatch, status_code):
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/test/token")
        mock_response = MagicMock(ok=True, status_code=status_code)

        with patch.object(mailDisco._default_session, "post", return_value=mock_response) as mock_post:
            result = MailDisco("【正常に書き込みました】\n利用先: テスト店").send()

        assert result is True
        mock_post.assert_called_once_with(
            "https://discord.com/api/webhooks/test/token",
            json={"content": "【正常に書き込みました】\n利用先: テスト店", "allowed_mentions": {"parse": []}},
            timeout=5,
        )

    @pytest.mark.parametrize("env_value", [None, ""])
    def test_send_skips_when_webhook_url_missing_or_empty(self, monkeypatch, caplog, env_value):
        if env_value is None:
            monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
        else:
            monkeypatch.setenv("DISCORD_WEBHOOK_URL", env_value)

        with patch.object(mailDisco._default_session, "post") as mock_post:
            with caplog.at_level(logging.WARNING, logger="smbc_card_recorder.src.mailDisco"):
                result = MailDisco("送信されないメッセージ").send()

        assert result is False
        mock_post.assert_not_called()
        assert "DISCORD_WEBHOOK_URL" in caplog.text

    @pytest.mark.parametrize("status_code", [400, 401, 404, 429, 500, 503])
    def test_send_logs_error_on_non_ok_http_status(self, monkeypatch, caplog, status_code):
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/test/token")
        mock_response = MagicMock(ok=False, status_code=status_code, headers={})
        mock_response.json.side_effect = ValueError("no json")

        with (
            patch.object(mailDisco._default_session, "post", return_value=mock_response),
            patch("src.mailDisco.time.sleep"),
        ):
            with caplog.at_level(logging.ERROR, logger="smbc_card_recorder.src.mailDisco"):
                result = MailDisco("エラーテスト").send()

        assert result is False
        assert f"ステータスコード: {status_code}" in caplog.text

    @pytest.mark.parametrize(
        "exc",
        [
            requests.exceptions.Timeout("timed out"),
            requests.exceptions.ConnectionError("connection refused"),
            requests.exceptions.HTTPError("http error"),
            requests.exceptions.RequestException("generic error"),
        ],
    )
    def test_send_catches_request_exceptions_and_logs_error(self, monkeypatch, caplog, exc):
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/test/token")

        with patch.object(mailDisco._default_session, "post", side_effect=exc):
            with caplog.at_level(logging.ERROR, logger="smbc_card_recorder.src.mailDisco"):
                result = MailDisco("例外テスト").send()

        assert result is False
        assert "Discord通知の送信中にエラーが発生しました" in caplog.text

    def test_send_preserves_special_characters_and_empty_string(self, monkeypatch):
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/test/token")
        mock_response = MagicMock(ok=True, status_code=204)

        with patch.object(mailDisco._default_session, "post", return_value=mock_response) as mock_post:
            assert MailDisco("").send() is True
            assert MailDisco("⚠️ 絵文字 & 記号 <> \"' \n改行").send() is True

        assert mock_post.call_count == 2
        assert mock_post.call_args_list[0].kwargs["json"] == {"content": "", "allowed_mentions": {"parse": []}}
        assert mock_post.call_args_list[1].kwargs["json"] == {"content": "⚠️ 絵文字 & 記号 <> \"' \n改行", "allowed_mentions": {"parse": []}}

    def test_extract_retry_after_fallback_branches(self):
        # JSON が非 dict の場合 + Retry-After ヘッダ無し -> デフォルト 1.0 秒
        resp_no_header = MagicMock(headers={})
        resp_no_header.json.return_value = ["not-a-dict"]
        assert DiscordNotifier._extract_retry_after(resp_no_header) == 1.0

        # JSON dict だが retry_after が None + Retry-After ヘッダが非数値 -> 1.0 秒
        resp_bad_header = MagicMock(headers={"Retry-After": "not-a-float"})
        resp_bad_header.json.return_value = {"retry_after": None}
        assert DiscordNotifier._extract_retry_after(resp_bad_header) == 1.0
