import requests
import os
from src.logger_config import setup_logger

logger = setup_logger(__name__)

class MailDisco:
    """
    Discord Webhookを使用してメッセージを通知するクラス。

    :ivar str message: 送信するメッセージの内容
    """
    def __init__(self,message):
        """
        MailDisco クラスのインスタンスを初期化する。

        :param message: Discordに送信するテキストメッセージ
        :type message: str
        """
        self.message = message
    def send(self):
        """
        環境変数からWebhook URLを取得し、メッセージをPOSTリクエストで送信する。
        """
        URL = os.getenv("DISCORD_WEBHOOK_URL")

        if not URL:
            logger.warning("環境変数 'DISCORD_WEBHOOK_URL' が設定されていません。Discord通知をスキップします。")
            return

        message_content = {
            "content":self.message
        }
        try:
            response = requests.post(URL, json=message_content, timeout=5)
            if not response.ok:
                logger.error("Discord通知の送信に失敗しました。ステータスコード: %d", response.status_code)
        except requests.exceptions.RequestException as e:
            logger.error("Discord通知の送信中にエラーが発生しました: %s", e)
