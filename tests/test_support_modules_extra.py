"""purpose / mailDisco / logger_config の追加回帰テスト（ログ出力内容・フォーマッタ・ファイル読み込み条件）。"""

import builtins
import logging
import os
import re
import sys
from logging.handlers import TimedRotatingFileHandler
from unittest.mock import MagicMock, patch

import pytest
import requests

import src.logger_config as logger_config
import src.mailDisco as mailDisco
import src.purpose as purpose_module
from src.mailDisco import MailDisco
from src.purpose import Purpose

PURPOSE_LOGGER = "smbc_card_recorder.src.purpose"
DISCO_LOGGER = "smbc_card_recorder.src.mailDisco"


def _setup_csv(monkeypatch, tmp_path, content):
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "config").mkdir(exist_ok=True)
    path = tmp_path / "config" / "categories.csv"
    path.write_text(content, encoding="utf-8")
    monkeypatch.setattr(
        purpose_module, "__file__", str(tmp_path / "src" / "purpose.py")
    )
    return path


class TestPurposeExtra:
    def test_reads_config_categories_csv_relative_to_repo_with_utf8_sig(
        self, monkeypatch, tmp_path
    ):
        path = _setup_csv(monkeypatch, tmp_path, "店\n")
        real_open = builtins.open
        seen = []

        def _spy(file, *args, **kwargs):
            seen.append((str(file), args, kwargs))
            return real_open(file, *args, **kwargs)

        with patch("builtins.open", side_effect=_spy):
            Purpose("店")
        assert seen == [(str(path), (), {"mode": "r", "encoding": "utf-8-sig"})]

    def test_missing_file_is_warning_not_error_and_open_not_attempted(
        self, monkeypatch, tmp_path, caplog
    ):
        (tmp_path / "src").mkdir()
        monkeypatch.setattr(
            purpose_module, "__file__", str(tmp_path / "src" / "purpose.py")
        )
        with (
            patch("builtins.open") as mock_open,
            caplog.at_level(logging.DEBUG, logger=PURPOSE_LOGGER),
        ):
            Purpose("店")
        mock_open.assert_not_called()
        assert [r.levelno for r in caplog.records] == [logging.WARNING]
        assert (
            caplog.records[0].getMessage()
            == "カテゴリファイル 'categories.csv' が見つかりません。"
        )

    def test_load_logs_count(self, monkeypatch, tmp_path, caplog):
        _setup_csv(monkeypatch, tmp_path, "a,b\nc\n")
        with caplog.at_level(logging.INFO, logger=PURPOSE_LOGGER):
            Purpose("a")
        assert "カテゴリ定義をロードしました: 3件" in [
            r.getMessage() for r in caplog.records
        ]

    def test_load_logs_count_zero_for_empty_csv(self, monkeypatch, tmp_path, caplog):
        _setup_csv(monkeypatch, tmp_path, "")
        with caplog.at_level(logging.INFO, logger=PURPOSE_LOGGER):
            assert Purpose("a").judge() is None
        assert Purpose._category_map == {}
        assert "カテゴリ定義をロードしました: 0件" in [
            r.getMessage() for r in caplog.records
        ]

    def test_skipped_row_warning_contains_row_content(
        self, monkeypatch, tmp_path, caplog
    ):
        _setup_csv(monkeypatch, tmp_path, "1\n2\n3\n4\n5\n超過A,超過B\n")
        with caplog.at_level(logging.WARNING, logger=PURPOSE_LOGGER):
            Purpose("1")
        messages = [r.getMessage() for r in caplog.records]
        assert (
            "6行目のデータはカテゴリ定義がないためスキップされました: ['超過A', '超過B']"
            in messages
        )

    def test_read_error_logged_with_traceback(self, monkeypatch, tmp_path, caplog):
        path = _setup_csv(monkeypatch, tmp_path, "")
        path.write_bytes(b"\x81\x00\xff\xfe")
        with caplog.at_level(logging.ERROR, logger=PURPOSE_LOGGER):
            Purpose("x")
        rec = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(rec) == 1
        assert rec[0].exc_info and rec[0].exc_info[0] is not None
        assert rec[0].getMessage() == "カテゴリ定義ファイルの読み込み中にエラーが発生しました"

    def test_quoted_csv_cells_with_commas(self, monkeypatch, tmp_path):
        _setup_csv(monkeypatch, tmp_path, '"店A, 本店",店B\n')
        assert Purpose("店A, 本店").judge() == "食費"
        assert Purpose("店B").judge() == "食費"

    def test_judge_logs_hit_and_miss(self, monkeypatch, tmp_path, caplog):
        _setup_csv(monkeypatch, tmp_path, "スーパーA\n")
        with caplog.at_level(logging.INFO, logger=PURPOSE_LOGGER):
            Purpose("スーパーA").judge()
            Purpose("不明店").judge()
        messages = [r.getMessage() for r in caplog.records]
        assert "'スーパーA' は 食費 カテゴリに属します。" in messages
        assert "'不明店' はどのカテゴリにも見つかりませんでした。" in messages

    def test_judge_with_non_string_location(self, monkeypatch, tmp_path):
        _setup_csv(monkeypatch, tmp_path, "100\n")
        assert Purpose(None).judge() is None
        assert Purpose(100).judge() is None

    def test_location_is_stored(self, monkeypatch, tmp_path):
        _setup_csv(monkeypatch, tmp_path, "")
        assert Purpose("店").location == "店"

    def test_cache_reused_when_mtime_unchanged_and_reloaded_when_mtime_changes(
        self, monkeypatch, tmp_path
    ):
        path = _setup_csv(monkeypatch, tmp_path, "旧店\n")
        os.utime(path, (1000, 1000))
        assert Purpose("旧店").judge() == "食費"

        # mtime が同じままならキャッシュを維持
        path.write_text("新店\n", encoding="utf-8")
        os.utime(path, (1000, 1000))
        assert Purpose("新店").judge() is None
        assert Purpose("旧店").judge() == "食費"

        # mtime が変化すると自動で再読み込みされる（Issue #29）
        os.utime(path, (2000, 2000))
        assert Purpose("新店").judge() == "食費"


class TestMailDiscoExtra:
    def test_webhook_url_is_read_at_send_time_not_init(self, monkeypatch):
        monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
        disco = MailDisco("msg")
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://example.invalid/hook")
        with patch.object(
            mailDisco._default_session, "post", return_value=MagicMock(ok=True)
        ) as post:
            disco.send()
        post.assert_called_once()
        assert post.call_args.args == ("https://example.invalid/hook",)

    def test_send_returns_bool(self, monkeypatch):
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://example.invalid/hook")
        with patch.object(mailDisco._default_session, "post", return_value=MagicMock(ok=True)):
            assert MailDisco("x").send() is True

    def test_success_logs_nothing(self, monkeypatch, caplog):
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://example.invalid/hook")
        with (
            patch.object(
                mailDisco._default_session,
                "post",
                return_value=MagicMock(ok=True, status_code=204),
            ),
            caplog.at_level(logging.DEBUG, logger=DISCO_LOGGER),
        ):
            MailDisco("x").send()
        assert caplog.records == []

    def test_missing_url_warning_text(self, monkeypatch, caplog):
        monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
        with caplog.at_level(logging.WARNING, logger=DISCO_LOGGER):
            MailDisco("x").send()
        assert [r.getMessage() for r in caplog.records] == [
            "環境変数 'DISCORD_WEBHOOK_URL' が設定されていません。Discord通知をスキップします。"
        ]

    def test_status_error_text_after_retries(self, monkeypatch, caplog):
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://example.invalid/hook")
        resp = MagicMock(ok=False, status_code=429, headers={})
        resp.json.side_effect = ValueError("no json")
        with (
            patch.object(mailDisco._default_session, "post", return_value=resp),
            patch("src.mailDisco.time.sleep"),
            caplog.at_level(logging.ERROR, logger=DISCO_LOGGER),
        ):
            assert MailDisco("x").send() is False
        assert [r.getMessage() for r in caplog.records] == [
            "Discord通知の送信に失敗しました。ステータスコード: 429"
        ]

    def test_exception_text_includes_reason(self, monkeypatch, caplog):
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://example.invalid/hook")
        with (
            patch.object(
                mailDisco._default_session,
                "post",
                side_effect=requests.exceptions.Timeout("read timed out"),
            ),
            caplog.at_level(logging.ERROR, logger=DISCO_LOGGER),
        ):
            assert MailDisco("x").send() is False
        assert [r.getMessage() for r in caplog.records] == [
            "Discord通知の送信中にエラーが発生しました: read timed out"
        ]

    def test_non_request_exceptions_propagate(self, monkeypatch):
        """RequestException 以外（プログラミングエラー等）は握りつぶさない。"""
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://example.invalid/hook")
        with (
            patch.object(mailDisco._default_session, "post", side_effect=ValueError("bad")),
            pytest.raises(ValueError),
        ):
            MailDisco("x").send()


@pytest.fixture
def isolated_logger(monkeypatch, tmp_path):
    name = "smbc_card_recorder_test_extra"
    monkeypatch.setattr(logger_config, "_ROOT_LOGGER_NAME", name)
    (tmp_path / "src").mkdir()
    monkeypatch.setattr(
        logger_config, "__file__", str(tmp_path / "src" / "logger_config.py")
    )
    root = logging.getLogger(name)
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()
    root.propagate = True
    yield name, tmp_path
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()
    root.propagate = True


class TestLoggerConfigExtra:
    def test_all_handlers_share_same_formatter_format(self, isolated_logger):
        logger = logger_config.setup_logger()
        for h in logger.handlers:
            assert h.formatter is not None
            assert (
                h.formatter._fmt == "%(asctime)s [%(levelname)s] [%(name)s] %(message)s"
            )
            assert h.formatter.datefmt == "%Y-%m-%d %H:%M:%S"

    def test_rotation_interval_is_daily(self, isolated_logger):
        logger = logger_config.setup_logger()
        rotating = [
            h for h in logger.handlers if isinstance(h, TimedRotatingFileHandler)
        ]
        assert len(rotating) == 2
        for h in rotating:
            assert h.interval == 24 * 60 * 60
            assert h.backupCount == 30

    def test_log_line_format(self, isolated_logger):
        name, tmp_path = isolated_logger
        logger_config.setup_logger("fmt").info("本文")
        for h in logging.getLogger(name).handlers:
            h.flush()
        line = (
            (tmp_path / "logs" / "app.log")
            .read_text(encoding="utf-8")
            .strip()
            .splitlines()[-1]
        )
        assert re.fullmatch(
            rf"\d{{4}}-\d{{2}}-\d{{2}} \d{{2}}:\d{{2}}:\d{{2}} \[INFO\] \[{name}\.fmt\] 本文",
            line,
        ), line

    def test_console_handler_writes_info_to_stderr_with_format(
        self, isolated_logger, capsys
    ):
        name, _ = isolated_logger
        child = logger_config.setup_logger("console")
        console = next(
            h
            for h in logging.getLogger(name).handlers
            if type(h) is logging.StreamHandler
        )
        assert console.level == logging.INFO
        assert console.stream is sys.stderr
        child.debug("見えないデバッグ")
        child.info("コンソール情報")
        err = capsys.readouterr().err
        assert "見えないデバッグ" not in err
        assert f"[INFO] [{name}.console] コンソール情報" in err

    def test_logs_dir_is_created_next_to_src(self, isolated_logger):
        _, tmp_path = isolated_logger
        assert not (tmp_path / "logs").exists()
        logger_config.setup_logger()
        assert (tmp_path / "logs").is_dir()

    def test_existing_logs_dir_is_ok(self, isolated_logger):
        _, tmp_path = isolated_logger
        (tmp_path / "logs").mkdir()
        logger_config.setup_logger()

    def test_child_loggers_propagate_to_root(self, isolated_logger):
        child = logger_config.setup_logger("prop")
        assert child.propagate is True
        assert child.getEffectiveLevel() == logging.DEBUG

    def test_error_log_contains_traceback_when_exc_info(self, isolated_logger):
        name, tmp_path = isolated_logger
        log = logger_config.setup_logger("tb")
        try:
            raise RuntimeError("トレース確認")
        except RuntimeError:
            log.error("失敗", exc_info=True)
        for h in logging.getLogger(name).handlers:
            h.flush()
        text = (tmp_path / "logs" / "error.log").read_text(encoding="utf-8")
        assert "Traceback (most recent call last)" in text
        assert "RuntimeError: トレース確認" in text
