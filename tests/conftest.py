import base64
import datetime
import io
import logging
import os
import runpy
import types
from unittest.mock import MagicMock, patch

import pytest

# src.mailSystem は import 時に setup_logger() を呼び、ロガーにハンドラが無ければ
# リポジトリ直下の logs/app.log・logs/error.log へ出力する FileHandler を張る。
# テスト実行で実運用ログを汚さないよう、import より前に NullHandler を登録しておく
# （logger_config._init_root_logger はハンドラが既にあれば何もしない）。
# caplog はルートロガーへの伝播で記録するため、この差し替えの影響を受けない。
logging.getLogger("smbc_card_recorder").addHandler(logging.NullHandler())

import src.mailSystem as mailSystem  # noqa: E402
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

    # load_dotenv() で .env から読み込まれた実 Webhook URL を隔離し、
    # モック漏れがあっても実 Discord チャンネルへ送信されないようにする
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)

    yield


class FixedDateTime(datetime.datetime):
    """now() だけを固定値に差し替えた datetime。"""

    fixed_now = datetime.datetime(2026, 11, 27, 9, 30, 0)

    @classmethod
    def now(cls, tz=None):
        return cls.fixed_now


@pytest.fixture
def fixed_now(monkeypatch):
    """
    mailSystem から見える datetime.datetime.now() を固定する。
    月末 23:59:59 を跨いで期待値と実測値がずれるフレーキーさを防ぐ。
    """
    monkeypatch.setattr(mailSystem, "datetime", types.SimpleNamespace(datetime=FixedDateTime))
    monkeypatch.setattr(FixedDateTime, "fixed_now", datetime.datetime(2026, 7, 31, 23, 59, 59))
    return FixedDateTime.fixed_now


# __main__ ブロック実行時に読み書きされる状態ファイル（config/ 配下）
STATE_FILES = frozenset({"gmailtoken.json", "last_run_time.json", "processed_message_ids.json"})


def run_main_block(
    *,
    json_files=None,
    init_ok=True,
    sleep_effects=(None, KeyboardInterrupt),
    gmail=None,
):
    """
    src/mailSystem.py の __main__ ブロックを安全に実行するヘルパー。

    runpy はモジュールを新しい名前空間で再実行するため、reset_global_state による
    TOKEN_FILE 等の差し替えが効かない。そこで状態ファイル（STATE_FILES）に限って
    存在判定・読み書きをフックし、それ以外のファイルへの書き込みはテスト失敗とする。

    :param json_files: {ファイル名: 内容(str)}。存在するものとして読み込ませる状態ファイル
    :return: 検証用モックをまとめた dict
    """
    json_files = json_files or {}
    assert set(json_files) <= STATE_FILES
    real_open = open
    real_exists = os.path.exists
    events = []

    def _exists(path):
        name = os.path.basename(str(path))
        if name in STATE_FILES:
            return name in json_files
        return real_exists(path)

    def _open(file, mode="r", *args, **kwargs):
        name = os.path.basename(str(file))
        writing = any(m in mode for m in "wax+")
        if name in STATE_FILES:
            if writing:
                events.append(("write", name))
                return io.StringIO()
            if name in json_files:
                events.append(("read", name))
                return io.StringIO(json_files[name])
        elif writing:
            raise AssertionError(f"__main__ テスト中に想定外のファイル書き込み: {file!r}")
        return real_open(file, mode, *args, **kwargs)

    if gmail is None:
        gmail = MagicMock()
        gmail.users().messages().list().execute.return_value = {"messages": []}

    disco_cls = MagicMock()
    creds = MagicMock(valid=True)
    creds.to_json.return_value = "{}"
    flow = MagicMock()
    flow.run_local_server.return_value = creds

    def _flow_factory(*a, **k):
        events.append(("oauth", None))
        if not init_ok:
            raise FileNotFoundError
        return flow

    with (
        patch("dotenv.load_dotenv") as load_dotenv,
        patch("src.mailDisco.MailDisco", disco_cls),
        patch(
            "google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file",
            side_effect=_flow_factory,
        ),
        patch("googleapiclient.discovery.build", return_value=gmail),
        patch("gspread.service_account", return_value=MagicMock()),
        patch("os.path.exists", side_effect=_exists),
        patch("builtins.open", side_effect=_open),
        patch.dict(os.environ, {"SERVICE_ACCOUNT_FILE": "dummy", "SPREADSHEET_ID": "dummy"}),
        patch(
            "schedule.run_pending",
            side_effect=lambda: events.append(("run_pending", None)),
        ) as run_pending,
        patch("time.sleep", side_effect=list(sleep_effects)) as sleep,
    ):
        runpy.run_path(mailSystem.__file__, run_name="__main__")

    return {
        "events": events,
        "disco_messages": [c.args[0] for c in disco_cls.call_args_list],
        "disco_cls": disco_cls,
        "load_dotenv": load_dotenv,
        "run_pending": run_pending,
        "sleep": sleep,
        "gmail": gmail,
    }
