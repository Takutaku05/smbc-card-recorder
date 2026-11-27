import logging
from unittest.mock import MagicMock, patch

import pytest
import requests

from src.mailDisco import MailDisco


class TestMailDisco:
    def test_init_stores_message(self):
        disco = MailDisco("テスト通知")
        assert disco.message == "テスト通知"

    @pytest.mark.parametrize("status_code", [200, 204])
    def test_send_success(self, monkeypatch, status_code):
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/test/token")
        mock_response = MagicMock(ok=True, status_code=status_code)

        with patch("src.mailDisco.requests.post", return_value=mock_response) as mock_post:
            MailDisco("【正常に書き込みました】\n利用先: テスト店").send()

        mock_post.assert_called_once_with(
            "https://discord.com/api/webhooks/test/token",
            json={"content": "【正常に書き込みました】\n利用先: テスト店"},
            timeout=5,
        )

    @pytest.mark.parametrize("env_value", [None, ""])
    def test_send_skips_when_webhook_url_missing_or_empty(self, monkeypatch, caplog, env_value):
        if env_value is None:
            monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
        else:
            monkeypatch.setenv("DISCORD_WEBHOOK_URL", env_value)

        with patch("src.mailDisco.requests.post") as mock_post:
            with caplog.at_level(logging.WARNING, logger="smbc_card_recorder.src.mailDisco"):
                MailDisco("送信されないメッセージ").send()

        mock_post.assert_not_called()
        assert "DISCORD_WEBHOOK_URL" in caplog.text

    @pytest.mark.parametrize("status_code", [400, 401, 404, 429, 500, 503])
    def test_send_logs_error_on_non_ok_http_status(self, monkeypatch, caplog, status_code):
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/test/token")
        mock_response = MagicMock(ok=False, status_code=status_code)

        with patch("src.mailDisco.requests.post", return_value=mock_response):
            with caplog.at_level(logging.ERROR, logger="smbc_card_recorder.src.mailDisco"):
                MailDisco("エラーテスト").send()

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

        with patch("src.mailDisco.requests.post", side_effect=exc):
            with caplog.at_level(logging.ERROR, logger="smbc_card_recorder.src.mailDisco"):
                # 例外が外部に伝播しないことを検証
                MailDisco("例外テスト").send()

        assert "Discord通知の送信中にエラーが発生しました" in caplog.text

    def test_send_preserves_special_characters_and_empty_string(self, monkeypatch):
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/test/token")
        mock_response = MagicMock(ok=True, status_code=204)

        with patch("src.mailDisco.requests.post", return_value=mock_response) as mock_post:
            MailDisco("").send()
            MailDisco("⚠️ 絵文字 & 記号 <> \"' \n改行").send()

        assert mock_post.call_count == 2
        assert mock_post.call_args_list[0].kwargs["json"] == {"content": ""}
        assert mock_post.call_args_list[1].kwargs["json"] == {"content": "⚠️ 絵文字 & 記号 <> \"' \n改行"}
