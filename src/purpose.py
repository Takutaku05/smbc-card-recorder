import csv
import os
from src.logger_config import setup_logger

logger = setup_logger(__name__)

class Purpose:
    """
    利用場所（店名）に基づき、外部CSVファイルを使用して用途カテゴリを分類するクラス。

    ``categories.csv`` を読み込み、行番号に基づいて以下の順序でカテゴリを割り当てる。

    1. 食費
    2. 外食費
    3. 生活雑貨
    4. 交通費
    5. その他

    :ivar list CATEGORY_ORDER: CSVの行順序に対応するカテゴリ名の定義リスト
    :ivar str CSV_FILE: 読み込むCSVファイル名（デフォルト: "categories.csv"）
    :cvar dict _category_map: {店名: カテゴリ名} のマッピングキャッシュ。初回インスタンス化時にロードされる。
    """
    
    CATEGORY_ORDER = [
        "食費",
        "外食費",
        "生活雑貨",
        "交通費",
        "その他",
    ]
    
    _category_map = None
    CSV_FILE = "categories.csv"

    def __init__(self, location):
        """
        Purpose クラスのインスタンスを初期化する。

        カテゴリ定義（_category_map）が未ロードの場合は、自動的に読み込み処理を実行する。

        :param location: 判定対象となる場所・店名
        :type location: str
        """
        self.location = location
        if type(self)._category_map is None:
            type(self)._load_categories()

    @classmethod
    def _load_categories(cls):
        """
        CSVファイルを読み込み、店名とカテゴリのマッピング辞書を作成する（クラスメソッド）。

        実行ファイルのディレクトリにある ``categories.csv`` を探す。
        各行に含まれる店名をキー、行番号に対応する ``CATEGORY_ORDER`` の値をバリューとして登録する。

        :return: なし
        :rtype: None
        :raises Exception: ファイル読み込み中に予期せぬエラーが発生した場合（エラー内容は標準出力される）。
        """
        cls._category_map = {}
        
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        file_path = os.path.join(base_dir, "config", cls.CSV_FILE)

        if not os.path.exists(file_path):
            logger.warning("カテゴリファイル '%s' が見つかりません。", cls.CSV_FILE)
            return

        try:
            with open(file_path, mode='r', encoding='utf-8') as f:
                reader = csv.reader(f)
                
                for i, row in enumerate(reader):
                    # 行番号が定義数を超えていたら無視（または拡張）
                    if i >= len(cls.CATEGORY_ORDER):
                        logger.warning("%d行目のデータはカテゴリ定義がないためスキップされました: %s", i + 1, row)
                        continue
                    
                    category = cls.CATEGORY_ORDER[i]
                    
                    for store in row:
                        store_name = store.strip()
                        if store_name: # 空文字でなければ登録
                            cls._category_map[store_name] = category
            
            logger.info("カテゴリ定義をロードしました: %d件", len(cls._category_map))

        except Exception as e:
            logger.error("カテゴリ定義ファイルの読み込み中にエラーが発生しました: %s", e, exc_info=True)

    def judge(self):
        """
        インスタンス変数の ``location`` がどのカテゴリに属するかを判定する。

        ロード済みのマップに対して完全一致で検索を行う。

        :return: 判定されたカテゴリ名（例: "食費"）。
                 マッピングに存在しない場合、またはマップのロードに失敗している場合は None を返す。
        :rtype: str | None
        """
        if Purpose._category_map is None:
            return None

        # 完全一致で検索
        if self.location in Purpose._category_map:
            category = Purpose._category_map[self.location]
            logger.info("'%s' は %s カテゴリに属します。", self.location, category)
            return category
        else:
            logger.info("'%s' はどのカテゴリにも見つかりませんでした。", self.location)
            return None
