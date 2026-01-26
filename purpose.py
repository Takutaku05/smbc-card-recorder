import csv
import os

class purpose:
    """
    利用場所によって用途を分類するクラス
    
    categories.csv の行番号に基づいてカテゴリを判定します。
    1行目: 食費
    2行目: 外食費
    3行目: 生活雑貨
    4行目: 交通費
    5行目: その他
    """
    
    # カテゴリの定義（CSVの行順に対応）
    CATEGORY_ORDER = [
        "食費",      # 1行目
        "外食費",    # 2行目
        "生活雑貨",  # 3行目
        "交通費",    # 4行目
        "その他"     # 5行目
    ]
    
    _category_map = None
    CSV_FILE = "categories.csv"

    def __init__(self, location):
        self.location = location
        if purpose._category_map is None:
            purpose._load_categories()

    @classmethod
    def _load_categories(cls):
        cls._category_map = {}
        
        base_dir = os.path.dirname(os.path.abspath(__file__))
        file_path = os.path.join(base_dir, cls.CSV_FILE)

        if not os.path.exists(file_path):
            print(f"⚠️ カテゴリファイル '{cls.CSV_FILE}' が見つかりません。")
            return

        try:
            with open(file_path, mode='r', encoding='utf-8') as f:
                reader = csv.reader(f)
                
                for i, row in enumerate(reader):
                    # 行番号が定義数を超えていたら無視（または拡張）
                    if i >= len(cls.CATEGORY_ORDER):
                        print(f"⚠️ {i+1}行目のデータはカテゴリ定義がないためスキップされました: {row}")
                        continue
                    
                    category = cls.CATEGORY_ORDER[i]
                    
                    for store in row:
                        store_name = store.strip()
                        if store_name: # 空文字でなければ登録
                            cls._category_map[store_name] = category
            
            print(f"ℹ️ カテゴリ定義をロードしました: {len(cls._category_map)}件")

        except Exception as e:
            print(f"❌ カテゴリ定義ファイルの読み込み中にエラーが発生しました: {e}")

    def judge(self):
        if purpose._category_map is None:
            return None

        # 完全一致で検索
        if self.location in purpose._category_map:
            category = purpose._category_map[self.location]
            print(f"'{self.location}' は {category} カテゴリに属します。")
            return category
        else:
            print(f"'{self.location}' はどのカテゴリにも見つかりませんでした。")
            return None