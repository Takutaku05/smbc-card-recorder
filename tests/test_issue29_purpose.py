import os

from src.purpose import Purpose


def _make_purpose_subclass(csv_path):
    class IsolatedPurpose(Purpose):
        _category_map = None
        _category_mtime = None

        @classmethod
        def _get_csv_path(cls):
            return str(csv_path)

    return IsolatedPurpose


def test_positional_csv_exact_and_blank_lines_do_not_shift(tmp_path):
    csv_file = tmp_path / "categories.csv"
    csv_file.write_text(
        "スーパーA,コンビニC\n\nレストランA\n   \nドラッグストアA\nJR東日本\nAmazon\n",
        encoding="utf-8",
    )
    cls = _make_purpose_subclass(csv_file)

    assert cls("スーパーA").judge() == "食費"
    assert cls("レストランA").judge() == "外食費"
    assert cls("ドラッグストアA").judge() == "生活雑貨"
    assert cls("JR東日本").judge() == "交通費"
    assert cls("Amazon").judge() == "その他"


def test_utf8_bom_csv_loads_first_entry_cleanly(tmp_path):
    csv_file = tmp_path / "categories.csv"
    csv_file.write_text("スーパーA,コンビニC\n", encoding="utf-8-sig")
    cls = _make_purpose_subclass(csv_file)

    assert cls("スーパーA").judge() == "食費"


def test_cp932_csv_loads_cleanly(tmp_path):
    csv_file = tmp_path / "categories.csv"
    csv_file.write_text("スーパー栄\nカフェ渋谷\n", encoding="cp932")
    cls = _make_purpose_subclass(csv_file)

    assert cls("スーパー栄").judge() == "食費"
    assert cls("カフェ渋谷").judge() == "外食費"


def test_failed_load_does_not_poison_cache_and_retries_when_fixed(tmp_path):
    csv_file = tmp_path / "categories.csv"
    # UTF-8 でも CP932 でもデコード失敗するバイト列
    csv_file.write_bytes(b"\x81\x00\xff\xfe")
    cls = _make_purpose_subclass(csv_file)

    assert cls("スーパーA").judge() is None
    assert cls._category_map is None

    csv_file.write_text("スーパーA\n", encoding="utf-8")
    assert cls("スーパーA").judge() == "食費"


def test_missing_csv_returns_none_and_loads_once_created(tmp_path):
    csv_file = tmp_path / "categories.csv"
    cls = _make_purpose_subclass(csv_file)

    assert cls("スーパーA").judge() is None

    csv_file.write_text("スーパーA\n", encoding="utf-8")
    assert cls("スーパーA").judge() == "食費"


def test_mtime_change_reloads_categories(tmp_path):
    csv_file = tmp_path / "categories.csv"
    csv_file.write_text("スーパーA\n", encoding="utf-8")
    os.utime(csv_file, (1000, 1000))
    cls = _make_purpose_subclass(csv_file)

    assert cls("レストランB").judge() is None

    csv_file.write_text("スーパーA\nレストランB\n", encoding="utf-8")
    os.utime(csv_file, (2000, 2000))
    assert cls("レストランB").judge() == "外食費"


def test_normalization_prefix_and_substring_priority(tmp_path):
    csv_file = tmp_path / "categories.csv"
    csv_file.write_text(
        "セブンイレブン,セブンイレブン 特別店\n"
        "カフェ\n"
        "ドラッグストアA\n"
        "JR東日本\n"
        "Amazon\n",
        encoding="utf-8",
    )
    cls = _make_purpose_subclass(csv_file)

    # NFKC + casefold
    assert cls("ＡＭＡＺＯＮ").judge() == "その他"
    # 前方一致
    assert cls("セブンイレブン／渋谷店").judge() == "食費"
    # 部分一致
    assert cls("東京 セブンイレブン渋谷").judge() == "食費"
    # 完全一致が最優先
    assert cls("セブンイレブン 特別店").judge() == "食費"
    # 一致しない場合は None
    assert cls("未知の店舗").judge() is None


def test_two_column_header_csv_format(tmp_path):
    csv_file = tmp_path / "categories.csv"
    csv_file.write_text(
        "store,category\nセブンイレブン,食費\nJR東日本,交通費\n",
        encoding="utf-8",
    )
    cls = _make_purpose_subclass(csv_file)

    assert cls("セブンイレブン 渋谷").judge() == "食費"
    assert cls("JR東日本").judge() == "交通費"


def test_subclass_isolation_does_not_read_parent_map(tmp_path):
    csv_file = tmp_path / "categories.csv"
    csv_file.write_text("テスト店舗\n", encoding="utf-8")
    cls = _make_purpose_subclass(csv_file)

    Purpose._category_map = {"親クラス店舗": "その他"}
    try:
        assert cls("テスト店舗").judge() == "食費"
        assert cls("親クラス店舗").judge() is None
    finally:
        Purpose._category_map = None
        Purpose._category_mtime = None
