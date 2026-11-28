import logging
import os
import sys
from logging.handlers import TimedRotatingFileHandler

_ROOT_LOGGER_NAME = "smbc_card_recorder"


def _chmod_best_effort(path, mode):
    """
    権限を変更する。所有者が異なる等で変更できない場合も起動を止めず、標準エラーへ警告のみ出力する。

    （ロガー初期化中のためロガーは使えない）
    """
    try:
        os.chmod(path, mode)
    except OSError as e:
        print(
            f"[WARNING] '{path}' の権限を {oct(mode)} に変更できませんでした: {e}",
            file=sys.stderr,
        )


class _RestrictedTimedRotatingFileHandler(TimedRotatingFileHandler):
    """ログファイルを 0600 パーミッションで作成・オープンするハンドラ。"""

    def _open(self):
        flags = os.O_CREAT | os.O_APPEND | os.O_WRONLY
        fd = os.open(self.baseFilename, flags, 0o600)
        _chmod_best_effort(self.baseFilename, 0o600)
        return os.fdopen(fd, self.mode, encoding=self.encoding, errors=self.errors)


def _init_root_logger():
    """親ロガーにハンドラを一度だけ設定する。"""
    logger = logging.getLogger(_ROOT_LOGGER_NAME)

    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)
    logger.propagate = False  # ルートロガーへ伝播させず二重出力を防ぐ

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
    os.makedirs(log_dir, mode=0o700, exist_ok=True)
    _chmod_best_effort(log_dir, 0o700)

    file_handler = _RestrictedTimedRotatingFileHandler(
        filename=os.path.join(log_dir, "app.log"),
        when="midnight",       # 毎日0時にローテーション
        interval=1,
        backupCount=30,        # 30日分保持
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    # --- エラーログ専用ハンドラ（ERROR以上のみ） ---
    error_handler = _RestrictedTimedRotatingFileHandler(
        filename=os.path.join(log_dir, "error.log"),
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8",
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(formatter)
    logger.addHandler(error_handler)

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
