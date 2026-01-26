import requests
from dotenv import load_dotenv
import os

class mailDisco:
    """
    Discord Webhookを使用してメッセージを通知するクラス。

    :ivar str message: 送信するメッセージの内容
    """
    def __init__(self,message):
        """
        mailDisco クラスのインスタンスを初期化する。

        :param message: Discordに送信するテキストメッセージ
        :type message: str
        """
        self.message = message
    def send(self):
        """
        環境変数からWebhook URLを取得し、メッセージをPOSTリクエストで送信する。
        """
        URL = os.getenv("DISCORD_WEBHOOK_URL")

        message_content = {
            "content":self.message
        }
        try:
            requests.post(URL,json=message_content)
        except requests.exceptions.RequestException as e:
            print("error")
            print(e)