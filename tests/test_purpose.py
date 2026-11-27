import logging
from unittest.mock import patch

import src.purpose as purpose_module
from src.purpose import Purpose


def _setup_csv(monkeypatch, tmp_path, content: str, filename: str = "categories.csv"):
    """tmp_path/config/categories.csv を作成し、purpose.__file__ を差し替える。"""
    fake_src = tmp_path / "src"
    fake_src.mkdir(exist_ok=True)
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)

    csv_path = config_dir / filename
    csv_path.write_text(content, encoding="utf-8")

    monkeypatch.setattr(purpose_module, "__file__", str(fake_src / "purpose.py"))
    return csv_path


class TestPurpose:
    def test_category_order_definition(self):
        assert Purpose.CATEGORY_ORDER == ["食費", "外食費", "生活雑貨", "交通費", "その他"]
        assert Purpose.CSV_FILE == "categories.csv"

    def test_judge_all_five_categories_from_csv(self, monkeypatch, tmp_path):
        csv_content = (
            "スーパーA,スーパーB,八百屋,コンビニC\n"
            "レストランA,居酒屋B,カフェC\n"
            "ドラッグストアA,ホームセンターB\n"
            "JR東日本,東京メトロ,タクシーA\n"
            "Amazon,楽天,その他店舗\n"
        )
        _setup_csv(monkeypatch, tmp_path, csv_content)

        assert Purpose("スーパーA").judge() == "食費"
        assert Purpose("コンビニC").judge() == "食費"
        assert Purpose("レストランA").judge() == "外食費"
        assert Purpose("カフェC").judge() == "外食費"
        assert Purpose("ドラッグストアA").judge() == "生活雑貨"
        assert Purpose("JR東日本").judge() == "交通費"
        assert Purpose("Amazon").judge() == "その他"
        assert Purpose("その他店舗").judge() == "その他"

    def test_judge_returns_none_for_unknown_store(self, monkeypatch, tmp_path):
        _setup_csv(monkeypatch, tmp_path, "スーパーA\nレストランA\n")

        assert Purpose("未知の店舗").judge() is None
        assert Purpose("").judge() is None
        assert Purpose("   ").judge() is None
        assert Purpose("N/A").judge() is None

    def test_judge_supports_nfkc_casefold_prefix_and_substring_match(self, monkeypatch, tmp_path):
        """Issue #29: NFKC正規化・大文字小文字無視のうえで完全一致・前方一致・部分一致を判定する。"""
        _setup_csv(monkeypatch, tmp_path, "Amazon,スターバックス\n")

        assert Purpose("Amazon").judge() == "食費"
        assert Purpose("amazon").judge() == "食費"
        assert Purpose("Ａｍａｚｏｎ.co.jp").judge() == "食費"
        assert Purpose("スターバックス 渋谷店").judge() == "食費"
        assert Purpose("東京 スターバックス 渋谷").judge() == "食費"

    def test_load_strips_whitespace_and_skips_empty_cells_and_blank_lines(self, monkeypatch, tmp_path):
        csv_content = "  スーパーA , ,   , スーパーB  \n\n  レストランA  \n"
        _setup_csv(monkeypatch, tmp_path, csv_content)

        p = Purpose("スーパーA")
        assert p.judge() == "食費"
        assert Purpose("スーパーB").judge() == "食費"
        # 空行はセパレータとしてスキップされ、次の非空行が2番目カテゴリ（外食費）になる
        assert Purpose("レストランA").judge() == "外食費"
        assert "" not in Purpose._category_map

    def test_two_column_csv_skips_short_and_incomplete_rows(self, monkeypatch, tmp_path):
        csv_content = (
            "store,category\n"
            "単一カラム行\n"
            ",食費\n"
            "空カテゴリ店,\n"
            "有効店舗,交通費\n"
        )
        _setup_csv(monkeypatch, tmp_path, csv_content)

        assert Purpose("有効店舗").judge() == "交通費"
        assert Purpose("単一カラム行").judge() is None
        assert Purpose("空カテゴリ店").judge() is None

    def test_load_skips_rows_beyond_category_order_with_warning(self, monkeypatch, tmp_path, caplog):
        csv_content = (
            "店1\n"
            "店2\n"
            "店3\n"
            "店4\n"
            "店5\n"
            "超過店6\n"
            "超過店7\n"
        )
        _setup_csv(monkeypatch, tmp_path, csv_content)

        with caplog.at_level(logging.WARNING, logger="smbc_card_recorder.src.purpose"):
            assert Purpose("店5").judge() == "その他"
            assert Purpose("超過店6").judge() is None
            assert Purpose("超過店7").judge() is None

        assert "6行目のデータはカテゴリ定義がないためスキップされました" in caplog.text
        assert "7行目のデータはカテゴリ定義がないためスキップされました" in caplog.text

    def test_duplicate_store_later_row_overwrites_earlier(self, monkeypatch, tmp_path):
        csv_content = "共通店\n共通店\n"
        _setup_csv(monkeypatch, tmp_path, csv_content)

        assert Purpose("共通店").judge() == "外食費"

    def test_category_map_is_cached_across_instances(self, monkeypatch, tmp_path):
        _setup_csv(monkeypatch, tmp_path, "スーパーA\n")

        with patch.object(Purpose, "_load_categories", wraps=Purpose._load_categories) as spy:
            assert Purpose("スーパーA").judge() == "食費"
            assert Purpose("スーパーA").judge() == "食費"
            assert Purpose("別店舗").judge() is None
            assert spy.call_count == 1

    def test_missing_csv_file_logs_warning_and_returns_none(self, monkeypatch, tmp_path, caplog):
        fake_src = tmp_path / "src"
        fake_src.mkdir()
        monkeypatch.setattr(purpose_module, "__file__", str(fake_src / "purpose.py"))

        with caplog.at_level(logging.WARNING, logger="smbc_card_recorder.src.purpose"):
            result = Purpose("スーパーA").judge()

        assert result is None
        assert Purpose._category_map is None
        assert "categories.csv" in caplog.text

    def test_read_error_is_caught_and_logged(self, monkeypatch, tmp_path, caplog):
        csv_path = _setup_csv(monkeypatch, tmp_path, "dummy")
        # UTF-8 でも CP932 でもデコードできない不正バイト列を書き込む
        csv_path.write_bytes(b"\x81\x00\xff\xfe")

        with caplog.at_level(logging.ERROR, logger="smbc_card_recorder.src.purpose"):
            result = Purpose("スーパーA").judge()

        assert result is None
        assert "カテゴリ定義ファイルの読み込み中にエラーが発生しました" in caplog.text

    def test_judge_returns_none_if_category_map_forced_to_none(self, monkeypatch, tmp_path):
        _setup_csv(monkeypatch, tmp_path, "スーパーA\n")
        p = Purpose("スーパーA")
        monkeypatch.setattr(Purpose, "_category_map", None)

        assert p.judge() is None
