import csv
import os
import unicodedata
from typing import ClassVar

from src.logger_config import setup_logger

logger = setup_logger(__name__)

class Purpose:
    """
    利用場所（店名）に基づき、外部CSVファイルを使用して用途カテゴリを分類するクラス。

    ``categories.csv`` を読み込み、行番号に基づいて以下の順序でカテゴリを割り当てる
    （1行目が ``store,category`` ヘッダの場合は2列形式として読み込む）。

    1. 食費
    2. 外食費
    3. 生活雑貨
    4. 交通費
    5. その他

    :ivar list CATEGORY_ORDER: CSVの行順序に対応するカテゴリ名の定義リスト
    :ivar str CSV_FILE: 読み込むCSVファイル名（デフォルト: "categories.csv"）
    :cvar dict _category_map: {正規化済み店名: カテゴリ名} のマッピングキャッシュ。初回インスタンス化時にロードされる。
    :cvar float | None _category_mtime: 最後に読み込んだCSVファイルの更新時刻
    """
    
    CATEGORY_ORDER: ClassVar[list[str]] = [
        "食費",
        "外食費",
        "生活雑貨",
        "交通費",
        "その他",
    ]
    
    _category_map = None
    _category_mtime = None
    CSV_FILE = "categories.csv"

    def __init__(self, location):
        """
        Purpose クラスのインスタンスを初期化する。

        カテゴリ定義（_category_map）が未ロード、またはCSVファイルが更新されている場合は、
        自動的に読み込み処理を実行する。

        :param location: 判定対象となる場所・店名
        :type location: str
        """
        self.location = location
        cls = type(self)
        file_path = cls._get_csv_path()
        current_mtime = os.path.getmtime(file_path) if os.path.exists(file_path) else None
        if cls._category_map is None or (
            current_mtime is not None
            and cls._category_mtime is not None
            and current_mtime != cls._category_mtime
        ):
            cls._load_categories()

    @staticmethod
    def _normalize(text):
        """
        比較用に文字列を NFKC 正規化・前後空白除去・小文字化する。

        :param text: 正規化対象の文字列
        :type text: str
        :return: 正規化後の文字列
        :rtype: str
        """
        return unicodedata.normalize("NFKC", text).strip().casefold()

    @classmethod
    def _get_csv_path(cls):
        """
        カテゴリ定義CSVファイルのパスを返す。

        :return: CSVファイルの絶対パス
        :rtype: str
        """
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return os.path.join(base_dir, "config", cls.CSV_FILE)

    @classmethod
    def _load_categories(cls):
        """
        CSVファイルを読み込み、店名とカテゴリのマッピング辞書を作成する（クラスメソッド）。

        ``config/categories.csv`` を UTF-8（BOM付き含む）または CP932 で読み込み、
        空行をスキップしたうえでマッピングを構築する。
        1行目が ``store,category`` のヘッダ行であれば2列形式、そうでなければ従来の行順形式で読み込む。
        読み込み失敗時はキャッシュを ``None`` に保ち、次回インスタンス化時に再試行できるようにする。

        :return: なし
        :rtype: None
        """
        file_path = cls._get_csv_path()

        if not os.path.exists(file_path):
            logger.warning("カテゴリファイル '%s' が見つかりません。", cls.CSV_FILE)
            cls._category_map = None
            cls._category_mtime = None
            return

        try:
            rows = None
            last_decode_error = None
            for encoding in ("utf-8-sig", "cp932"):
                try:
                    with open(file_path, mode='r', encoding=encoding) as f:
                        rows = list(csv.reader(f))
                    break
                except UnicodeDecodeError as e:
                    last_decode_error = e

            if rows is None:
                raise last_decode_error

            non_empty_rows = [
                row for row in rows if row and any(cell.strip() for cell in row)
            ]

            new_map = {}
            is_two_col = (
                bool(non_empty_rows)
                and len(non_empty_rows[0]) >= 2
                and non_empty_rows[0][0].strip().lower() == "store"
                and non_empty_rows[0][1].strip().lower() == "category"
            )

            if is_two_col:
                for row in non_empty_rows[1:]:
                    if len(row) < 2:
                        continue
                    store_name = row[0].strip()
                    category = row[1].strip()
                    if store_name and category:
                        new_map[cls._normalize(store_name)] = category
            else:
                for category_idx, row in enumerate(non_empty_rows):
                    # 行番号が定義数を超えていたら無視（または拡張）
                    if category_idx >= len(cls.CATEGORY_ORDER):
                        logger.warning("%d行目のデータはカテゴリ定義がないためスキップされました: %s", category_idx + 1, row)
                        continue

                    category = cls.CATEGORY_ORDER[category_idx]

                    for store in row:
                        store_name = store.strip()
                        if store_name: # 空文字でなければ登録
                            new_map[cls._normalize(store_name)] = category

            cls._category_map = new_map
            cls._category_mtime = os.path.getmtime(file_path)
            logger.info("カテゴリ定義をロードしました: %d件", len(cls._category_map))

        except Exception:
            logger.exception("カテゴリ定義ファイルの読み込み中にエラーが発生しました")
            cls._category_map = None
            cls._category_mtime = None

    def judge(self):
        """
        インスタンス変数の ``location`` がどのカテゴリに属するかを判定する。

        NFKC正規化・大文字小文字無視のうえで、完全一致 → 前方一致 → 部分一致の順に検索する。

        :return: 判定されたカテゴリ名（例: "食費"）。
                 マッピングに存在しない場合、またはマップのロードに失敗している場合は None を返す。
        :rtype: str | None
        """
        category_map = type(self)._category_map
        if not category_map or not isinstance(self.location, str) or not self.location.strip():
            return None

        normalized_location = self._normalize(self.location)

        # 1. 完全一致
        if normalized_location in category_map:
            category = category_map[normalized_location]
            logger.info("'%s' は %s カテゴリに属します。", self.location, category)
            return category

        # 2. 前方一致（最長一致を優先）
        prefix_matches = [k for k in category_map if normalized_location.startswith(k)]
        if prefix_matches:
            best_key = max(prefix_matches, key=len)
            category = category_map[best_key]
            logger.info("'%s' は %s カテゴリに属します（前方一致: '%s'）。", self.location, category, best_key)
            return category

        # 3. 部分一致（最長一致を優先）
        substring_matches = [k for k in category_map if k in normalized_location]
        if substring_matches:
            best_key = max(substring_matches, key=len)
            category = category_map[best_key]
            logger.info("'%s' は %s カテゴリに属します（部分一致: '%s'）。", self.location, category, best_key)
            return category

        logger.info("'%s' はどのカテゴリにも見つかりませんでした。", self.location)
        return None
