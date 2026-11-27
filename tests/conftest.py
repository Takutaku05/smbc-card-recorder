import base64
import datetime
import logging
import os
import runpy
import signal
import tempfile
import types
from pathlib import Path
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
    全テストでグローバル変数・ファイルパス・キャッシュを隔離する。
    本番の config/ ファイルや認証情報に一切触れないことを保証する。
    """
    monkeypatch.setattr(mailSystem, "TOKEN_FILE", str(tmp_path / "gmailtoken.json"))
    monkeypatch.setattr(mailSystem, "LAST_RUN_FILE", str(tmp_path / "last_run_time.json"))
    monkeypatch.setattr(mailSystem, "PROCESSED_IDS_FILE", str(tmp_path / "processed_message_ids.json"))
    monkeypatch.setattr(mailSystem, "FAILED_ATTEMPTS_FILE", str(tmp_path / "failed_message_attempts.json"))
    monkeypatch.setattr(mailSystem, "SPREADSHEET_ID", "test-spreadsheet-id")
    monkeypatch.setattr(mailSystem, "SERVICE_ACCOUNT_FILE", str(tmp_path / "service_account.json"))
    monkeypatch.setattr(mailSystem, "gc", None)
    monkeypatch.setattr(mailSystem, "gmail_service", None)
    monkeypatch.setattr(mailSystem, "processed_ids", set())
    monkeypatch.setattr(mailSystem, "_processed_id_timestamps", {})
    monkeypatch.setattr(mailSystem, "_message_failure_counts", {})
    monkeypatch.setattr(mailSystem, "_shutdown_requested", False)
    monkeypatch.setattr(mailSystem, "_cached_gc", None)
    monkeypatch.setattr(mailSystem, "_cached_spreadsheet_id", None)
    monkeypatch.setattr(mailSystem, "_cached_spreadsheet", None)
    monkeypatch.setattr(mailSystem, "_worksheet_cache", {})
    monkeypatch.setattr(mailSystem, "_gmail_creds", None)
    monkeypatch.setattr(mailSystem, "_last_saved_token_json", None)
    monkeypatch.setattr(mailSystem, "_refresh_error_notified", False)

    # Purpose のクラス変数キャッシュをリセット
    monkeypatch.setattr(Purpose, "_category_map", None)
    monkeypatch.setattr(Purpose, "_category_mtime", None)

    # load_dotenv() で .env から読み込まれた実 Webhook URL を隔離し、
    # モック漏れがあっても実 Discord チャンネルへ送信されないようにする
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)

    yield


class FixedDateTime(datetime.datetime):
    """now() だけを固定値に差し替えた datetime。"""

    fixed_now = datetime.datetime(2026, 11, 27, 9, 30, 0)

    @classmethod
    def now(cls, tz=None):
        if tz is not None:
            if cls.fixed_now.tzinfo is None:
                return cls.fixed_now.replace(tzinfo=tz)
            return cls.fixed_now.astimezone(tz)
        return cls.fixed_now


def make_fixed_datetime_namespace():
    """mailSystem.datetime 差し替え用の名前空間（timezone / timedelta を保持）。"""
    return types.SimpleNamespace(
        datetime=FixedDateTime,
        timezone=datetime.timezone,
        timedelta=datetime.timedelta,
    )


@pytest.fixture
def fixed_now(monkeypatch):
    """
    mailSystem から見える datetime.datetime.now() を固定する。
    月末 23:59:59 を跨いで期待値と実測値がずれるフレーキーさを防ぐ。
    """
    monkeypatch.setattr(mailSystem, "datetime", make_fixed_datetime_namespace())
    monkeypatch.setattr(FixedDateTime, "fixed_now", datetime.datetime(2026, 7, 31, 23, 59, 59))
    return FixedDateTime.fixed_now


@pytest.fixture
def pinned_job_now(monkeypatch):
    """
    check_mail_job の基準時刻を 2026-11-27 09:30:00 UTC に固定する。

    Gmail 検索範囲は「現在時刻 - SEARCH_WINDOW_DAYS」で下限が切られるため、固定日付の
    last_run_time を使うテストが実時間の経過で失敗しない（タイムボムにならない）ようにする。
    """
    monkeypatch.setattr(mailSystem, "datetime", make_fixed_datetime_namespace())
    monkeypatch.setattr(FixedDateTime, "fixed_now", datetime.datetime(2026, 11, 27, 9, 30, 0))
    return datetime.datetime(2026, 11, 27, 9, 30, 0, tzinfo=datetime.timezone.utc)


def expected_earliest_after(now_utc):
    """検索範囲の下限（after: に入る epoch 秒）を返す。"""
    return int((now_utc - datetime.timedelta(days=mailSystem.SEARCH_WINDOW_DAYS)).timestamp())


# __main__ ブロック実行時に読み書きされる状態ファイル（config/ 配下）
STATE_FILES = frozenset(
    {"gmailtoken.json", "last_run_time.json", "processed_message_ids.json", "failed_message_attempts.json"}
)


def run_main_block(
    *,
    json_files=None,
    init_ok=True,
    sleep_effects=(None, KeyboardInterrupt),
    gmail=None,
    on_sleep=None,
    service_account_side_effect=None,
):
    """
    src/mailSystem.py の __main__ ブロックを一時ディレクトリ隔離のもと安全に実行するヘルパー。

    runpy はモジュールを新しい名前空間で再実行するため、mailSystem.__file__ の abspath 解決先だけ
    一時サンドボックス配下に差し替え、実リポジトリの config/ への書き込みを完全に遮断する。
    """
    json_files = json_files or {}
    assert set(json_files) <= STATE_FILES

    real_open = open
    real_os_open = os.open
    real_join = os.path.join
    real_mail_system_file = os.path.abspath(mailSystem.__file__)
    repo_base_dir = os.path.dirname(os.path.dirname(real_mail_system_file))
    prev_sigterm = signal.getsignal(signal.SIGTERM)
    events = []
    exit_code = None

    with tempfile.TemporaryDirectory() as sandbox_str:
        sandbox = Path(sandbox_str)
        config_dir = sandbox / "config"
        config_dir.mkdir(mode=0o700)
        for name, content in json_files.items():
            (config_dir / name).write_text(content, encoding="utf-8")

        sandbox_prefix = str(sandbox.resolve())

        def _join(a, *p):
            if os.path.abspath(str(a)) == repo_base_dir and p and (
                p[0] == "config" or str(p[0]).startswith("config/") or str(p[0]).startswith("config\\")
            ):
                return real_join(str(sandbox), *p)
            return real_join(a, *p)

        def _normalize_state_name(filename):
            base = os.path.basename(str(filename))
            return base[:-4] if base.endswith(".tmp") else base

        def _open(file, mode="r", *args, **kwargs):
            writing = any(m in mode for m in "wax+")
            target = str(Path(file).resolve()) if isinstance(file, (str, os.PathLike)) else ""
            base = _normalize_state_name(file)
            if writing:
                if not target.startswith(sandbox_prefix):
                    raise AssertionError(f"__main__ テスト中に想定外のファイル書き込み: {file!r}")
                if base in STATE_FILES:
                    events.append(("write", base))
            elif base in STATE_FILES and target.startswith(sandbox_prefix):
                events.append(("read", base))
            return real_open(file, mode, *args, **kwargs)

        def _os_open(path, flags, mode=0o777, *args, **kwargs):
            writing = bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
            target = str(Path(path).resolve())
            base = _normalize_state_name(path)
            if writing:
                if not target.startswith(sandbox_prefix):
                    raise AssertionError(f"__main__ テスト中に想定外の os.open 書き込み: {path!r}")
                if base in STATE_FILES:
                    events.append(("write", base))
            return real_os_open(path, flags, mode, *args, **kwargs)

        if gmail is None:
            gmail = MagicMock()
            gmail.users().messages().list().execute.return_value = {"messages": []}

        disco_cls = MagicMock()
        creds = MagicMock(valid=True)
        creds.to_json.return_value = "{}"
        flow = MagicMock()
        flow.run_local_server.return_value = creds

        def _flow_factory(*_a, **_k):
            events.append(("oauth", None))
            if not init_ok:
                raise FileNotFoundError
            return flow

        try:
            with (
                patch("dotenv.load_dotenv") as load_dotenv,
                patch("src.mailDisco.MailDisco", disco_cls),
                patch(
                    "google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file",
                    side_effect=_flow_factory,
                ),
                patch("googleapiclient.discovery.build", return_value=gmail),
                patch(
                    "gspread.service_account",
                    return_value=MagicMock(),
                    side_effect=service_account_side_effect,
                ),
                patch("os.path.join", side_effect=_join),
                patch("builtins.open", side_effect=_open),
                patch("os.open", side_effect=_os_open),
                patch.dict(
                    os.environ,
                    {"SERVICE_ACCOUNT_FILE": "config/service_account.json", "SPREADSHEET_ID": "dummy"},
                ),
                patch(
                    "schedule.run_pending",
                    side_effect=lambda: events.append(("run_pending", None)),
                ) as run_pending,
                patch(
                    "time.sleep",
                    side_effect=on_sleep if on_sleep is not None else list(sleep_effects),
                ) as sleep,
            ):
                try:
                    runpy.run_path(real_mail_system_file, run_name="__main__")
                except SystemExit as exc:
                    exit_code = exc.code
        finally:
            signal.signal(signal.SIGTERM, prev_sigterm)

    return {
        "events": events,
        "exit_code": exit_code,
        "disco_messages": [c.args[0] for c in disco_cls.call_args_list],
        "disco_cls": disco_cls,
        "load_dotenv": load_dotenv,
        "run_pending": run_pending,
        "sleep": sleep,
        "gmail": gmail,
    }
