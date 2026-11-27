import logging
import os
from logging.handlers import TimedRotatingFileHandler

import pytest

import src.logger_config as logger_config


@pytest.fixture
def isolated_root_logger(monkeypatch, tmp_path):
    """
    テスト専用のルートロガー名とディレクトリを使い、
    テスト終了後にハンドラを確実にクローズ・クリーンアップする。
    """
    test_logger_name = "smbc_card_recorder_test_isolated"
    monkeypatch.setattr(logger_config, "_ROOT_LOGGER_NAME", test_logger_name)

    # logger_config.__file__ を tmp_path/src/logger_config.py に差し替えて
    # logs/ が tmp_path/logs に作られるようにする
    fake_src_dir = tmp_path / "src"
    fake_src_dir.mkdir()
    monkeypatch.setattr(logger_config, "__file__", str(fake_src_dir / "logger_config.py"))

    root = logging.getLogger(test_logger_name)
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()

    yield test_logger_name, tmp_path

    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()


class TestLoggerConfig:
    def test_setup_logger_without_name_returns_root_logger(self, isolated_root_logger):
        test_logger_name, _ = isolated_root_logger

        logger = logger_config.setup_logger()

        assert logger.name == test_logger_name
        assert logger.level == logging.DEBUG
        assert len(logger.handlers) == 3

    def test_setup_logger_with_name_returns_child_logger(self, isolated_root_logger):
        test_logger_name, _ = isolated_root_logger

        child = logger_config.setup_logger("my_module")

        assert child.name == f"{test_logger_name}.my_module"
        assert child.parent.name == test_logger_name
        # 子ロガー自身にはハンドラを追加せず、親ロガーのハンドラを共有する
        assert len(child.handlers) == 0
        assert len(child.parent.handlers) == 3

    def test_init_root_logger_is_idempotent(self, isolated_root_logger):
        test_logger_name, _ = isolated_root_logger

        first = logger_config.setup_logger()
        handlers_first = list(first.handlers)
        second = logger_config.setup_logger("sub")
        third = logger_config.setup_logger()

        assert third is first
        assert second.parent is first
        assert list(third.handlers) == handlers_first
        assert len(third.handlers) == 3

    def test_handler_types_levels_and_rotation_settings(self, isolated_root_logger):
        _, tmp_path = isolated_root_logger

        logger = logger_config.setup_logger()
        log_dir = tmp_path / "logs"

        assert log_dir.is_dir()

        stream_handlers = [
            h for h in logger.handlers
            if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
        ]
        rotating_handlers = [
            h for h in logger.handlers
            if isinstance(h, TimedRotatingFileHandler)
        ]

        assert len(stream_handlers) == 1
        assert stream_handlers[0].level == logging.INFO

        assert len(rotating_handlers) == 2
        by_basename = {os.path.basename(h.baseFilename): h for h in rotating_handlers}
        assert set(by_basename.keys()) == {"app.log", "error.log"}

        app_handler = by_basename["app.log"]
        assert app_handler.level == logging.DEBUG
        assert app_handler.when == "MIDNIGHT"
        assert app_handler.backupCount == 30
        assert app_handler.encoding == "utf-8"

        error_handler = by_basename["error.log"]
        assert error_handler.level == logging.ERROR
        assert error_handler.when == "MIDNIGHT"
        assert error_handler.backupCount == 30
        assert error_handler.encoding == "utf-8"

    def test_log_routing_between_app_log_and_error_log(self, isolated_root_logger):
        _, tmp_path = isolated_root_logger

        child = logger_config.setup_logger("routing_test")
        child.debug("デバッグメッセージ")
        child.info("情報メッセージ")
        child.warning("警告メッセージ")
        child.error("エラーメッセージ")
        child.critical("致命的メッセージ")

        for h in child.parent.handlers:
            h.flush()

        app_log = (tmp_path / "logs" / "app.log").read_text(encoding="utf-8")
        error_log = (tmp_path / "logs" / "error.log").read_text(encoding="utf-8")

        # app.log には DEBUG 以上すべてが出力される
        for msg in ("デバッグメッセージ", "情報メッセージ", "警告メッセージ", "エラーメッセージ", "致命的メッセージ"):
            assert msg in app_log

        # error.log には ERROR 以上のみが出力される
        assert "デバッグメッセージ" not in error_log
        assert "情報メッセージ" not in error_log
        assert "警告メッセージ" not in error_log
        assert "エラーメッセージ" in error_log
        assert "致命的メッセージ" in error_log

        # フォーマット検証: [LEVEL] [logger_name] message
        assert "[ERROR] [smbc_card_recorder_test_isolated.routing_test] エラーメッセージ" in error_log
