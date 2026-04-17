import logging
import os
from logging.handlers import TimedRotatingFileHandler

def setup_logger(name="smbc_card_recorder"):
    """
    アプリケーション共通のロガーをセットアップする。

    コンソール出力と日付ローテーション付きファイル出力の両方を設定する。
    ログファイルはプロジェクトルートの ``logs/`` ディレクトリに出力される。

    :param name: ロガー名（デフォルト: "smbc_card_recorder"）
    :type name: str
    :return: 設定済みのロガーインスタンス
    :rtype: logging.Logger
    """
    logger = logging.getLogger(name)

    # 既にハンドラが設定されている場合は再設定しない
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    # ログ出力フォーマット
    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # --- コンソールハンドラ ---
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # --- ファイルハンドラ（日付ローテーション） ---
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    log_dir = os.path.join(base_dir, "logs")
    os.makedirs(log_dir, exist_ok=True)

    file_handler = TimedRotatingFileHandler(
        filename=os.path.join(log_dir, "app.log"),
        when="midnight",       # 毎日0時にローテーション
        interval=1,
        backupCount=30,        # 30日分保持
        encoding="utf-8",
    )
    file_handler.suffix = "%Y-%m-%d"  # ローテーション後のファイル名: app.log.2026-04-17
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger
