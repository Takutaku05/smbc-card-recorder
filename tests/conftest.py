import base64
import pytest

import src.mailSystem as mailSystem
from src.purpose import Purpose


def encode_b64url(text: str, encoding: str = "utf-8", strip_padding: bool = False) -> str:
    """テスト用の URL-safe Base64 エンコードヘルパー。"""
    encoded = base64.urlsafe_b64encode(text.encode(encoding)).decode("ascii")
    if strip_padding:
        return encoded.rstrip("=")
    return encoded


@pytest.fixture(autouse=True)
def reset_global_state(monkeypatch, tmp_path):
    """
    全テストでグローバル変数・ファイルパス・Purposeキャッシュを隔離する。
    本番の config/ ファイルや認証情報に一切触れないことを保証する。
    """
    # mailSystem のグローバル状態とパスを tmp_path 配下に隔離
    monkeypatch.setattr(mailSystem, "TOKEN_FILE", str(tmp_path / "gmailtoken.json"))
    monkeypatch.setattr(mailSystem, "LAST_RUN_FILE", str(tmp_path / "last_run_time.json"))
    monkeypatch.setattr(mailSystem, "PROCESSED_IDS_FILE", str(tmp_path / "processed_message_ids.json"))
    monkeypatch.setattr(mailSystem, "SPREADSHEET_ID", "test-spreadsheet-id")
    monkeypatch.setattr(mailSystem, "SERVICE_ACCOUNT_FILE", str(tmp_path / "sheet.json"))
    monkeypatch.setattr(mailSystem, "gc", None)
    monkeypatch.setattr(mailSystem, "gmail_service", None)
    monkeypatch.setattr(mailSystem, "processed_ids", set())

    # Purpose のクラス変数キャッシュをリセット
    monkeypatch.setattr(Purpose, "_category_map", None)

    yield
