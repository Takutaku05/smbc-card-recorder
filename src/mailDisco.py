import os
import time

import requests

from src.logger_config import setup_logger

logger = setup_logger(__name__)

MAX_MESSAGE_LENGTH = 2000
DEFAULT_TIMEOUT = 5
DEFAULT_MAX_RETRIES = 3
MAX_RETRY_AFTER_SECONDS = 60.0

_default_session = requests.Session()


class DiscordNotifier:
    """
    Discord Webhookを使用してメッセージを通知するクライアントクラス。

    2000文字超過時の切り詰めおよび HTTP 429 レート制限時のリトライに対応する。
    """

    def __init__(
        self,
        webhook_url=None,
        session=None,
        timeout=DEFAULT_TIMEOUT,
        max_retries=DEFAULT_MAX_RETRIES,
    ):
        """
        DiscordNotifier クラスのインスタンスを初期化する。

        :param webhook_url: Discord Webhook URL（省略時は環境変数 DISCORD_WEBHOOK_URL を参照）
        :type webhook_url: str | None
        :param session: 再利用する requests.Session インスタンス
        :type session: requests.Session | None
        :param timeout: HTTPリクエストのタイムアウト秒数
        :type timeout: float | int
        :param max_retries: HTTP 429 応答時の最大リトライ回数
        :type max_retries: int
        """
        self.webhook_url = webhook_url
        self.session = session if session is not None else _default_session
        self.timeout = timeout
        self.max_retries = max_retries

    @staticmethod
    def _truncate_message(message):
        """
        Discord の 2000 文字上限に収まるようメッセージを切り詰める。

        :param message: 送信対象メッセージ
        :type message: str
        :return: 2000文字以内に整形された文字列
        :rtype: str
        """
        if len(message) <= MAX_MESSAGE_LENGTH:
            return message
        return message[: MAX_MESSAGE_LENGTH - 3] + "..."

    @staticmethod
    def _extract_retry_after(response):
        """
        HTTP 429 レスポンスから待機秒数を取り出す。

        :param response: requests のレスポンスオブジェクト
        :return: 待機秒数（上限 MAX_RETRY_AFTER_SECONDS）
        :rtype: float
        """
        wait_seconds = 1.0
        try:
            body = response.json()
            if isinstance(body, dict) and body.get("retry_after") is not None:
                wait_seconds = float(body["retry_after"])
                return max(0.0, min(wait_seconds, MAX_RETRY_AFTER_SECONDS))
        except (ValueError, TypeError):
            pass

        header_value = response.headers.get("Retry-After")
        if header_value is not None:
            try:
                wait_seconds = float(header_value)
            except ValueError:
                wait_seconds = 1.0

        return max(0.0, min(wait_seconds, MAX_RETRY_AFTER_SECONDS))

    def send(self, message):
        """
        Webhook URL 宛てにメッセージをPOSTリクエストで送信する。

        :param message: Discordに送信するテキストメッセージ
        :type message: str
        :return: 送信に成功した場合 True、失敗またはスキップ時は False
        :rtype: bool
        """
        webhook_url = self.webhook_url or os.getenv("DISCORD_WEBHOOK_URL")

        if not webhook_url:
            logger.warning("環境変数 'DISCORD_WEBHOOK_URL' が設定されていません。Discord通知をスキップします。")
            return False

        payload = {
            "content": self._truncate_message(str(message)),
        }

        try:
            for attempt in range(self.max_retries + 1):
                response = self.session.post(webhook_url, json=payload, timeout=self.timeout)
                if response.ok:
                    return True

                if response.status_code == 429 and attempt < self.max_retries:
                    wait_seconds = self._extract_retry_after(response)
                    logger.warning(
                        "Discord Webhook のレート制限(429)に達しました。%.2f秒後に再試行します (%d/%d)。",
                        wait_seconds,
                        attempt + 1,
                        self.max_retries,
                    )
                    time.sleep(wait_seconds)
                    continue

                logger.error("Discord通知の送信に失敗しました。ステータスコード: %d", response.status_code)
                return False
        except requests.exceptions.RequestException as e:
            logger.error("Discord通知の送信中にエラーが発生しました: %s", e)
            return False

        return False


class MailDisco:
    """
    Discord Webhookを使用してメッセージを通知する後方互換ラッパークラス。

    :ivar str message: 送信するメッセージの内容
    """

    def __init__(self, message, webhook_url=None, session=None):
        """
        MailDisco クラスのインスタンスを初期化する。

        :param message: Discordに送信するテキストメッセージ
        :type message: str
        :param webhook_url: 明示指定する Webhook URL（省略時は環境変数参照）
        :type webhook_url: str | None
        :param session: 再利用する requests.Session インスタンス
        :type session: requests.Session | None
        """
        self.message = message
        self._notifier = DiscordNotifier(webhook_url=webhook_url, session=session)

    def send(self):
        """
        メッセージをPOSTリクエストで送信する。

        :return: 送信成功時 True、失敗・スキップ時 False
        :rtype: bool
        """
        return self._notifier.send(self.message)
