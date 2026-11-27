import datetime
import json
import logging
from unittest.mock import patch

import src.mailSystem as mailSystem


class TestLastRunTimePersistence:
    def test_get_last_run_time_returns_none_when_file_missing(self):
        assert mailSystem.get_last_run_time() is None

    def test_save_and_get_last_run_time_roundtrip_naive(self):
        expected = datetime.datetime(2026, 5, 15, 14, 30, 45, 123456)
        mailSystem.save_current_run_time(expected)

        loaded = mailSystem.get_last_run_time()
        assert loaded == expected

    def test_save_and_get_last_run_time_roundtrip_aware(self):
        tz = datetime.timezone(datetime.timedelta(hours=9))
        expected = datetime.datetime(2026, 5, 15, 14, 30, 0, tzinfo=tz)
        mailSystem.save_current_run_time(expected)

        loaded = mailSystem.get_last_run_time()
        assert loaded == expected

    def test_get_last_run_time_corrupted_json_returns_none(self, tmp_path, caplog):
        (tmp_path / "last_run_time.json").write_text("{broken json", encoding="utf-8")

        with caplog.at_level(logging.WARNING, logger="smbc_card_recorder.src.mailSystem"):
            result = mailSystem.get_last_run_time()

        assert result is None
        assert "前回の実行日時ファイルが不正です" in caplog.text

    def test_get_last_run_time_missing_key_returns_none(self, tmp_path, caplog):
        (tmp_path / "last_run_time.json").write_text(json.dumps({"other": "val"}), encoding="utf-8")

        with caplog.at_level(logging.WARNING, logger="smbc_card_recorder.src.mailSystem"):
            result = mailSystem.get_last_run_time()

        assert result is None
        assert "前回の実行日時ファイルが不正です" in caplog.text

    def test_get_last_run_time_invalid_iso_string_returns_none(self, tmp_path, caplog):
        (tmp_path / "last_run_time.json").write_text(
            json.dumps({"last_run_time": "not-a-datetime"}), encoding="utf-8"
        )

        with caplog.at_level(logging.WARNING, logger="smbc_card_recorder.src.mailSystem"):
            result = mailSystem.get_last_run_time()

        assert result is None
        assert "前回の実行日時ファイルが不正です" in caplog.text


class TestProcessedIdsPersistence:
    def test_load_processed_ids_when_file_missing_sets_empty_set(self, caplog):
        mailSystem.processed_ids = {"stale-id"}

        with caplog.at_level(logging.INFO, logger="smbc_card_recorder.src.mailSystem"):
            mailSystem.load_processed_ids()

        assert mailSystem.processed_ids == set()
        assert "見つかりません" in caplog.text

    def test_save_and_load_processed_ids_roundtrip(self):
        mailSystem.processed_ids = {"msg_1", "msg_2", "msg_3"}
        mailSystem.save_processed_ids()

        mailSystem.processed_ids = set()
        mailSystem.load_processed_ids()

        assert mailSystem.processed_ids == {"msg_1", "msg_2", "msg_3"}

    def test_load_processed_ids_deduplicates_list(self, tmp_path):
        (tmp_path / "processed_message_ids.json").write_text(
            json.dumps(["id1", "id2", "id1", "id2"]), encoding="utf-8"
        )

        mailSystem.load_processed_ids()
        assert mailSystem.processed_ids == {"id1", "id2"}

    def test_load_processed_ids_corrupted_json_resets_to_empty(self, tmp_path, caplog):
        mailSystem.processed_ids = {"existing"}
        (tmp_path / "processed_message_ids.json").write_text("not json [", encoding="utf-8")

        with caplog.at_level(logging.WARNING, logger="smbc_card_recorder.src.mailSystem"):
            mailSystem.load_processed_ids()

        assert mailSystem.processed_ids == set()
        assert "不正です" in caplog.text

    def test_load_processed_ids_non_iterable_json_resets_to_empty(self, tmp_path, caplog):
        (tmp_path / "processed_message_ids.json").write_text("12345", encoding="utf-8")

        with caplog.at_level(logging.WARNING, logger="smbc_card_recorder.src.mailSystem"):
            mailSystem.load_processed_ids()

        assert mailSystem.processed_ids == set()
        assert "不正です" in caplog.text

    def test_save_processed_ids_io_error_is_caught_and_logged(self, caplog):
        mailSystem.processed_ids = {"msg_1"}

        with patch("builtins.open", side_effect=IOError("disk full")):
            with caplog.at_level(logging.ERROR, logger="smbc_card_recorder.src.mailSystem"):
                mailSystem.save_processed_ids()

        assert "処理済みメールIDの保存中にエラーが発生しました" in caplog.text
