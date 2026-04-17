import logging
import os
from logging.handlers import TimedRotatingFileHandler

_ROOT_LOGGER_NAME = "smbc_card_recorder"


def _init_root_logger():
    """親ロガーにハンドラを一度だけ設定する。"""
    logger = logging.getLogger(_ROOT_LOGGER_NAME)

    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

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
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger


def setup_logger(name=None):
    """
    アプリケーション共通のロガーを取得する。

    親ロガー（smbc_card_recorder）にハンドラを一度だけ設定し、
    各モジュールは子ロガーとしてハンドラを共有する。

    :param name: モジュール名。指定時は子ロガーを返す。
    :type name: str | None
    :return: 設定済みのロガーインスタンス
    :rtype: logging.Logger
    """
    _init_root_logger()

    if name:
        return logging.getLogger(f"{_ROOT_LOGGER_NAME}.{name}")

    return logging.getLogger(_ROOT_LOGGER_NAME)
