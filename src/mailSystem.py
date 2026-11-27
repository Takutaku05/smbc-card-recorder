import os.path
import signal
import time
import datetime
import json
import base64
import html
import re
from dotenv import load_dotenv

import src.mailDisco as mailDisco
import src.purpose as purpose
from src.logger_config import setup_logger

import schedule

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials as GmailCredentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import gspread

# --- ロガーの初期化 ---
logger = setup_logger(__name__)

# --- グローバル設定 ---
load_dotenv()
SCOPES_GMAIL = "https://www.googleapis.com/auth/gmail.readonly"
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN_FILE = os.path.join(BASE_DIR, "config", "gmailtoken.json")
LAST_RUN_FILE = os.path.join(BASE_DIR, "config", "last_run_time.json")
PROCESSED_IDS_FILE = os.path.join(BASE_DIR, "config", "processed_message_ids.json")
FAILED_ATTEMPTS_FILE = os.path.join(BASE_DIR, "config", "failed_message_attempts.json")
PROCESSED_IDS_MAX_ENTRIES = 2000
PROCESSED_IDS_RETENTION_DAYS = 90
# Gmail 検索範囲の上限（日数）。処理済みIDの保持日数より短くし、パージ済みIDのメールが
# 再検索されて二重記録されることを防ぐ（処理時刻 >= メール受信時刻のため、この範囲のメールのIDは必ず保持されている）
SEARCH_WINDOW_DAYS = PROCESSED_IDS_RETENTION_DAYS - 1
# 同一メールの処理失敗をこの回数まで再試行し、超えたら処理済みにして再試行・通知を止める（30分間隔で約3時間）
MAX_MESSAGE_ATTEMPTS = 6
GMAIL_QUERY_BASE = 'subject:"ご利用のお知らせ【三井住友カード】" -in:trash -in:spam -in:sent -in:drafts'
_processed_id_timestamps = {}
_message_failure_counts = {}
# SIGTERM 受信時に立てる停止要求フラグ（シグナルハンドラ内では I/O を行わない）
_shutdown_requested = False


def _resolve_config_path(file_path):
    """
    設定ファイルパスが相対パスの場合、プロジェクトルート (BASE_DIR) 基準に解決する。

    :param file_path: 環境変数等で指定されたファイルパス
    :type file_path: str | None
    :return: 解決済みパス（未指定時は None）
    :rtype: str | None
    """
    if file_path and not os.path.isabs(file_path):
        return os.path.join(BASE_DIR, file_path)
    return file_path


SERVICE_ACCOUNT_FILE = _resolve_config_path(os.getenv("SERVICE_ACCOUNT_FILE"))
SCOPES_SHEET = ["https://www.googleapis.com/auth/spreadsheets"]
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")

# --- グローバル変数 ---
gc = None
processed_ids = set()
gmail_service = None
_cached_gc = None
_cached_spreadsheet_id = None
_cached_spreadsheet = None
_worksheet_cache = {}
_gmail_creds = None
_last_saved_token_json = None
_refresh_error_notified = False

# sheet() の戻り値: 抽出失敗(N/A)のため記録をスキップした（再試行しても結果が変わらないため処理済み扱いにする）
SHEET_SKIPPED = "skipped"

# sheet() の書き込み失敗通知を送るか。check_mail_job が再試行中のメールでは False にし、
# Sheets 障害中に「滞留メール数 × 再試行回数」の通知が届かないようにする（打ち切り時は別途通知される）
_sheet_failure_notify_enabled = True


def _notify_sheet_failure(message):
    """sheet() の書き込み失敗を Discord に通知する（再試行中のメールでは送らずログのみ）。"""
    if _sheet_failure_notify_enabled:
        mailDisco.MailDisco(message).send()
    else:
        logger.info("再試行中のメールのため書き込み失敗の通知を省略しました。")


class BoundedBackOffHTTPClient(gspread.http_client.HTTPClient):
    """
    再試行回数に上限を設けた gspread 用 HTTP クライアント。

    gspread 標準の ``BackOffHTTPClient`` は再試行回数に上限がなく、Sheets API が
    5xx を返し続けるとジョブが停止し続けるため、本クラスで置き換える。

    * 429（レート制限）: リクエストは処理されていないため、メソッドを問わず再試行する。
    * 408 / 5xx: サーバー側で書き込み済みの可能性があるため、冪等なメソッド（GET / PUT 等）のみ再試行する
      （``append_row`` の POST を再試行すると同じ行が二重追加され得る）。
    """

    MAX_RETRIES = 5
    BASE_WAIT_SECONDS = 2
    MAX_WAIT_SECONDS = 32
    _IDEMPOTENT_METHODS = frozenset({"get", "head", "options", "put", "delete"})

    @classmethod
    def _should_retry(cls, method, code):
        if code == 429:
            return True
        if code == 408 or code >= 500:
            return str(method).lower() in cls._IDEMPOTENT_METHODS
        return False

    def request(self, method, endpoint, *args, **kwargs):
        attempt = 0
        while True:
            try:
                return super().request(method, endpoint, *args, **kwargs)
            except gspread.exceptions.APIError as err:
                # HTML の 502/503/504 等で JSON を解析できないと err.code は -1 になるため、HTTP ステータスで補う
                status_code = (
                    err.code
                    if isinstance(err.code, int) and err.code > 0
                    else getattr(err.response, "status_code", -1)
                )
                if attempt >= self.MAX_RETRIES or not self._should_retry(
                    method, status_code
                ):
                    raise
                wait_seconds = min(
                    self.BASE_WAIT_SECONDS * (2**attempt), self.MAX_WAIT_SECONDS
                )
                attempt += 1
                logger.warning(
                    "Sheets API がエラー (%s) を返しました。%d秒後に再試行します (%d/%d)。",
                    status_code,
                    wait_seconds,
                    attempt,
                    self.MAX_RETRIES,
                )
                time.sleep(wait_seconds)


def _extract_charset(part):
    """
    Gmail APIパートの Content-Type ヘッダから文字セット名を取得する。

    :param part: Gmail APIメッセージパート辞書
    :type part: dict
    :return: 小文字の文字セット名。指定がない場合は None
    :rtype: str | None
    """
    for header in part.get("headers", []):
        if header.get("name", "").lower() == "content-type":
            match = re.search(
                r'charset\s*=\s*"?([^";\s]+)"?', header.get("value", ""), re.IGNORECASE
            )
            if match:
                return match.group(1).strip().lower()
    return None


def decode_base64_url_safe(data, charset=None):
    """
    URLセーフなBase64エンコードデータをデコードする。

    パディング（=）の調整を行い、指定charset・ESCシーケンス判定（iso-2022-jp）・
    各種日本語文字コード（utf-8 / iso-2022-jp / cp932 / euc_jp）の順にデコードを試みる。

    :param data: Base64エンコードされた文字列
    :type data: str
    :param charset: Content-Type ヘッダ等から取得した優先文字セット
    :type charset: str | None
    :return: デコードされた文字列
    :rtype: str
    :raises UnicodeDecodeError: すべての候補文字コードでデコードに失敗した場合
    """
    decoded_bytes = base64.urlsafe_b64decode(data + "=" * ((-len(data)) % 4))

    candidates = []
    if charset:
        candidates.append(charset)
    # ISO-2022-JP は7bit文字コードのため、UTF-8より先にESCバイト有無で判定する
    if b"\x1b" in decoded_bytes:
        candidates.append("iso-2022-jp")
    candidates.extend(["utf-8", "iso-2022-jp", "euc_jp", "cp932"])

    tried = set()
    last_error = None
    for encoding in candidates:
        key = encoding.lower()
        if key in tried:
            continue
        tried.add(key)
        try:
            return decoded_bytes.decode(encoding)
        except (LookupError, UnicodeDecodeError) as err:
            if isinstance(err, UnicodeDecodeError):
                last_error = err

    raise last_error


def _html_to_text(html_body):
    """
    HTML文字列からスクリプト・スタイル・タグを除去し、テキストへ変換する。

    :param html_body: HTML本文
    :type html_body: str
    :return: プレーンテキスト化された文字列
    :rtype: str
    """
    # <head>（<title> 等）は表示されない領域のため、本文として抽出しない
    text = re.sub(
        r"<head\b[^>]*>.*?</head>", "", html_body, flags=re.IGNORECASE | re.DOTALL
    )
    text = re.sub(
        r"<style\b[^>]*>.*?</style>", "", text, flags=re.IGNORECASE | re.DOTALL
    )
    text = re.sub(
        r"<script\b[^>]*>.*?</script>", "", text, flags=re.IGNORECASE | re.DOTALL
    )
    # HTML ソース上の改行・インデントは表示に影響しないため空白1つへ正規化し、
    # 整形済みテーブル（<td>ご利用場所：</td>\n  <td>店名</td>）で項目名と値が別行に分かれないようにする
    text = re.sub(r"\s+", " ", text)
    # <br class="..."> のような属性付きの <br> も改行にする
    text = re.sub(
        r"<br\b[^>]*>|</tr>|</p>|</div>|</li>", "\n", text, flags=re.IGNORECASE
    )
    text = re.sub(r"</t[dh]>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text)


def _summarize_mime_tree(payload):
    """
    デバッグログ向けにペイロードの MIME 構造を要約する（本文内容は含めない）。

    :param payload: Gmail APIメッセージペイロード辞書
    :type payload: dict
    :return: MIME階層の文字列表現
    :rtype: str
    """
    mime_type = payload.get("mimeType") or "unknown"
    parts = payload.get("parts")
    if parts:
        children = ", ".join(_summarize_mime_tree(part) for part in parts)
        return f"{mime_type}[{children}]"
    return mime_type


def _extract_body_parts(payload):
    """
    Gmail APIのペイロード構造を再帰的に探索し、(text/plain, text/html) を返す。

    :param payload: Gmail APIから取得したメッセージのペイロード辞書
    :type payload: dict
    :return: (プレーンテキスト本文, HTML本文) のタプル
    :rtype: tuple[str, str]
    """
    parts = payload.get("parts")
    if parts:
        first_html = ""
        for part in parts:
            plain_body, html_body = _extract_body_parts(part)
            if plain_body:
                return plain_body, ""
            if html_body and not first_html:
                first_html = html_body
        return "", first_html

    if payload.get("filename"):
        return "", ""

    data = (payload.get("body") or {}).get("data")
    if not data:
        return "", ""

    # 本文として扱うのは text/plain（mimeType 未指定を含む）と text/html のみ。
    # text/calendar・text/csv 等の補助パートを本文として先取りしないようスキップする
    mime_type = (payload.get("mimeType") or "").lower()
    if mime_type not in ("", "text/plain", "text/html"):
        return "", ""

    decoded = decode_base64_url_safe(data, charset=_extract_charset(payload))
    if mime_type == "text/html":
        return "", decoded
    return decoded, ""


def get_email_body(msg_payload):
    """
    Gmail APIのペイロード構造からメール本文（プレーンテキストまたはHTML）を抽出・デコードする。

    ネストされた multipart 構造を再帰的に探索し、``text/plain`` を優先、
    存在しない場合は ``text/html`` をテキスト化して返す。

    :param msg_payload: Gmail APIから取得したメッセージのペイロード辞書
    :type msg_payload: dict
    :return: デコードされたメール本文。抽出できない場合は空文字を返す。
    :rtype: str
    """
    plain_body, html_body = _extract_body_parts(msg_payload)
    if plain_body:
        return plain_body
    if html_body:
        return _html_to_text(html_body)

    logger.warning(
        "本文パートを抽出できませんでした。MIME構造: %s",
        _summarize_mime_tree(msg_payload),
    )
    return ""


def _atomic_write_json(path, obj):
    """
    JSONファイルを一時ファイル経由（fsync + os.replace）でアトミックに書き込む。

    :param path: 保存先ファイルパス
    :type path: str
    :param obj: JSONシリアライズ対象のオブジェクト
    """
    parent_dir = os.path.dirname(path)
    if parent_dir:
        os.makedirs(parent_dir, exist_ok=True)
    tmp_path = f"{path}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(obj, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


LAST_RUN_OVERLAP_SECONDS = 300


def get_last_run_time():
    """
    前回の実行日時をJSONファイルから読み込む（UTC aware datetime に正規化）。

    :return: 前回実行日時。ファイルが存在しないか破損している場合は None
    :rtype: datetime.datetime | None
    """
    if os.path.exists(LAST_RUN_FILE):
        try:
            with open(LAST_RUN_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if not isinstance(data, dict):
                    raise TypeError(
                        "last_run_time.json のルート要素はオブジェクトである必要があります。"
                    )
                dt = datetime.datetime.fromisoformat(data.get("last_run_time"))
                return dt.astimezone(datetime.timezone.utc)
        except (json.JSONDecodeError, TypeError, ValueError, OSError) as e:
            logger.warning(
                "前回の実行日時ファイルが不正です (%s)。新しく作成します。", e
            )
            mailDisco.MailDisco(
                "⚠️ 前回の実行日時ファイルが破損または読み込み不能のためリセットしました。"
            ).send()
            return None
    return None


def save_current_run_time(current_time):
    """
    現在の実行日時を UTC aware ISO形式でJSONファイルに保存する。

    :param current_time: 保存する日時オブジェクト
    :type current_time: datetime.datetime
    """
    try:
        aware_utc = current_time.astimezone(datetime.timezone.utc)
        _atomic_write_json(LAST_RUN_FILE, {"last_run_time": aware_utc.isoformat()})
    except OSError as e:
        logger.error("前回実行日時の保存中にエラーが発生しました: %s", e)


def load_processed_ids():
    """
    処理済みのメールIDリストをファイルから読み込み、グローバル変数 ``processed_ids`` を更新する。
    重複処理を防ぐために使用される。
    """
    global processed_ids, _processed_id_timestamps
    if os.path.exists(PROCESSED_IDS_FILE):
        try:
            with open(PROCESSED_IDS_FILE, "r", encoding="utf-8") as f:
                raw_data = json.load(f)

            now = datetime.datetime.now(datetime.timezone.utc)
            now_iso = now.isoformat()
            if isinstance(raw_data, list):
                ts_map = {str(mid): now_iso for mid in raw_data}
            elif isinstance(raw_data, dict):
                ts_map = {str(k): str(v) for k, v in raw_data.items()}
            else:
                raise TypeError("processed_message_ids.json の形式が不正です。")

            pruned = _prune_processed_timestamps(ts_map, now=now)
            _processed_id_timestamps = pruned
            processed_ids = set(pruned.keys())
            logger.info("処理済みメールIDを %d 件ロードしました。", len(processed_ids))
        except (json.JSONDecodeError, TypeError, OSError) as e:
            logger.warning(
                "処理済みメールIDファイル '%s' が不正です。新しく作成します。エラー: %s",
                PROCESSED_IDS_FILE,
                e,
            )
            mailDisco.MailDisco(
                "⚠️ 処理済みメールIDファイルが破損または読み込み不能のため初期化しました。重複記録に注意してください。"
            ).send()
            processed_ids = set()
            _processed_id_timestamps = {}
    else:
        logger.info(
            "処理済みメールIDファイル '%s' が見つかりません。新しく作成します。",
            PROCESSED_IDS_FILE,
        )
        processed_ids = set()
        _processed_id_timestamps = {}


def _prune_processed_timestamps(ts_map, now=None):
    """
    保持日数と最大件数の上限を超えた古い処理済みIDエントリを除去する。

    :param ts_map: {message_id: iso_timestamp} 辞書
    :type ts_map: dict[str, str]
    :param now: 基準時刻（省略時は現在UTC時刻）
    :type now: datetime.datetime | None
    :return: パージ後の辞書
    :rtype: dict[str, str]
    """
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    cutoff = now - datetime.timedelta(days=PROCESSED_IDS_RETENTION_DAYS)

    kept = []
    for mid, ts_str in ts_map.items():
        try:
            parsed_dt = datetime.datetime.fromisoformat(ts_str).astimezone(
                datetime.timezone.utc
            )
        except (ValueError, TypeError):
            parsed_dt = now
            ts_str = now.isoformat()
        if parsed_dt >= cutoff:
            kept.append((parsed_dt, mid, ts_str))

    kept.sort(key=lambda item: item[0], reverse=True)
    kept = kept[:PROCESSED_IDS_MAX_ENTRIES]
    return {mid: ts_str for _dt, mid, ts_str in kept}


def save_processed_ids():
    """
    現在の処理済みメールIDセット（``processed_ids``）をJSONファイルに保存する。
    """
    global processed_ids, _processed_id_timestamps
    try:
        now = datetime.datetime.now(datetime.timezone.utc)
        now_iso = now.isoformat()
        merged = {
            mid: _processed_id_timestamps.get(mid, now_iso) for mid in processed_ids
        }
        pruned = _prune_processed_timestamps(merged, now=now)
        _processed_id_timestamps = pruned
        processed_ids.intersection_update(pruned.keys())
        _atomic_write_json(PROCESSED_IDS_FILE, _processed_id_timestamps)
    except OSError as e:
        logger.error("処理済みメールIDの保存中にエラーが発生しました: %s", e)


def load_message_failure_counts():
    """
    メールごとの連続処理失敗回数をファイルから読み込み、``_message_failure_counts`` を更新する。

    ファイルが存在しない・破損している場合は空の状態から開始する（失敗回数のリセットのみで実害はない）。
    """
    global _message_failure_counts
    _message_failure_counts = {}
    if not os.path.exists(FAILED_ATTEMPTS_FILE):
        return
    try:
        with open(FAILED_ATTEMPTS_FILE, "r", encoding="utf-8") as f:
            raw_data = json.load(f)
        if not isinstance(raw_data, dict):
            raise TypeError("failed_message_attempts.json の形式が不正です。")
        _message_failure_counts = {
            str(mid): int(count) for mid, count in raw_data.items()
        }
    except (json.JSONDecodeError, TypeError, ValueError, OSError) as e:
        logger.warning(
            "処理失敗回数ファイル '%s' が不正なため初期化します: %s",
            FAILED_ATTEMPTS_FILE,
            e,
        )
        _message_failure_counts = {}


def save_message_failure_counts():
    """メールごとの連続処理失敗回数をJSONファイルへ保存する。"""
    try:
        _atomic_write_json(FAILED_ATTEMPTS_FILE, _message_failure_counts)
    except OSError as e:
        logger.error("処理失敗回数の保存中にエラーが発生しました: %s", e)


def _record_message_failure(message_id, subject, reason, notify_first=True):
    """
    メールの処理失敗を記録し、上限回数に達した場合は処理済みにして再試行を打ち切る。

    初回の失敗時と打ち切り時のみ Discord に通知し、30分ごとの重複通知を抑止する。

    :param message_id: GmailメッセージID
    :type message_id: str
    :param subject: メール件名（通知用）
    :type subject: str
    :param reason: 失敗理由（通知用）
    :type reason: str
    :param notify_first: 初回失敗時に通知するか（呼び出し元で詳細通知済みの場合は False）
    :return: 再試行を打ち切って処理済みにした場合 True、次回再試行する場合 False
    :rtype: bool
    """
    count = _message_failure_counts.get(message_id, 0) + 1
    if count >= MAX_MESSAGE_ATTEMPTS:
        _message_failure_counts.pop(message_id, None)
        processed_ids.add(message_id)
        save_processed_ids()
        logger.error(
            "メールID %s は %d 回連続で処理に失敗したため、再試行を打ち切り処理済みにしました。",
            message_id,
            count,
        )
        mailDisco.MailDisco(
            f"❌ メールID {message_id}（件名: {subject}）は {count} 回連続で処理に失敗したため、"
            f"再試行を打ち切りました。\n最後の失敗理由: {reason}\n"
            "Gmail で該当メールを確認し、必要に応じて手動で記録してください。"
        ).send()
        return True

    _message_failure_counts[message_id] = count
    logger.warning(
        "メールID %s の処理に失敗しました (%d/%d回目): %s",
        message_id,
        count,
        MAX_MESSAGE_ATTEMPTS,
        reason,
    )
    if count == 1 and notify_first:
        mailDisco.MailDisco(
            f"⚠️ メールID {message_id}（件名: {subject}）の処理に失敗しました: {reason}\n"
            f"次回以降も再試行し、{MAX_MESSAGE_ATTEMPTS} 回連続で失敗した場合は打ち切ります。"
        ).send()
    return False


def system(mes_body):
    """
    メール本文を正規表現で解析し、主要な家計簿データ（日時、場所、金額）を抽出する。

    抽出後は ``sheet`` 関数を呼び出してスプレッドシートへの記録を行う。

    :param mes_body: 解析対象のメール本文テキスト
    :type mes_body: str
    :return: 書き込み成功時 True、失敗時 False、抽出失敗でスキップした場合 ``SHEET_SKIPPED``
    :rtype: bool | str
    """
    parsed = parse_mail_body(mes_body)
    logger.info(
        "抽出結果: 利用日=%s, 利用先=%s, 利用金額=%s",
        parsed["datetime"],
        parsed["location"],
        parsed["amount"],
    )
    return sheet(parsed["datetime"], parsed["location"], parsed["amount"])


def sheet(email_time, location, money):
    """
    抽出されたデータをGoogleスプレッドシートに書き込む。

    1. 抽出データに欠損（"N/A"）がある場合は警告し、Discordに通知を送る。
    2. ``Purpose`` クラスを使用して、利用場所からカテゴリ（食費、交通費など）を自動判定する。
    3. 対象月のシート（例: "11月"）が存在するか確認し、なければ "Sheet1" にフォールバックする。
    4. シートの空いている行を探してデータを追記する。

    :param email_time: 利用日時文字列
    :type email_time: str
    :param location: 利用場所・店名
    :type location: str
    :param money: 利用金額（整数または抽出失敗時は文字列）
    :type money: int | str
    :return: 書き込み成功時 True、失敗時 False、抽出失敗(N/A)でスキップした場合 ``SHEET_SKIPPED``
    :rtype: bool | str
    """
    global gc, _cached_gc, _cached_spreadsheet_id, _cached_spreadsheet, _worksheet_cache

    if gc is None:
        logger.error("GSpread認証が完了していません。データを書き込めません。")
        return False

    try:
        if (
            _cached_spreadsheet is None
            or _cached_gc is not gc
            or _cached_spreadsheet_id != SPREADSHEET_ID
        ):
            spreadsheet = gc.open_by_key(SPREADSHEET_ID)
            _cached_gc = gc
            _cached_spreadsheet_id = SPREADSHEET_ID
            _cached_spreadsheet = spreadsheet
            _worksheet_cache = {}
        else:
            spreadsheet = _cached_spreadsheet
        missing_fields = []
        if email_time == "N/A":
            missing_fields.append("利用日")
        if location == "N/A":
            missing_fields.append("利用先")
        if money == "N/A" or not isinstance(money, int):
            missing_fields.append("利用金額")

        if missing_fields:
            missing_str = "・".join(missing_fields)
            logger.warning(
                "抽出に失敗した項目があるため書き込みを中止します (%s): 利用日=%s, 利用先=%s, 金額=%s",
                missing_str,
                email_time,
                location,
                money,
            )
            mailDisco.MailDisco(
                f"⚠️ 以下の項目を抽出できなかったため記録を中止しました: {missing_str}\n"
                f"利用日: {email_time} / 利用先: {location} / 金額: {money}"
            ).send()
            # 同じ本文を再試行しても抽出結果は変わらないため、失敗(False)ではなくスキップとして返す
            return SHEET_SKIPPED

        sheet_name = timesheet(email_time)

        actual_sheet_name = sheet_name
        used_fallback_sheet = False

        try:
            if sheet_name in _worksheet_cache:
                worksheet = _worksheet_cache[sheet_name]
            else:
                worksheet = spreadsheet.worksheet(sheet_name)
                _worksheet_cache[sheet_name] = worksheet
        except gspread.exceptions.WorksheetNotFound:
            logger.error("ワークシート '%s' が見つかりません。", sheet_name)
            try:
                logger.info("フォールバックとして 'Sheet1' への書き込みを試みます。")
                if "Sheet1" in _worksheet_cache:
                    worksheet = _worksheet_cache["Sheet1"]
                else:
                    worksheet = spreadsheet.worksheet("Sheet1")
                    _worksheet_cache["Sheet1"] = worksheet
                actual_sheet_name = "Sheet1"
                used_fallback_sheet = True
            except gspread.exceptions.WorksheetNotFound:
                logger.error(
                    "ワークシート 'Sheet1' も見つかりません。書き込みを中止します。"
                )
                _notify_sheet_failure(
                    f"❌ ワークシート '{sheet_name}' も 'Sheet1' も見つからず、利用先: {location} / 金額: {money} を記録できませんでした。"
                )
                return False

        def _notify_write_success(written_pos):
            if used_fallback_sheet:
                mailDisco.MailDisco(
                    f"⚠️ ワークシート '{sheet_name}' が見つからず、'Sheet1' ({written_pos}) に書き込みました。\n利用先: {location} / 金額: {money}"
                ).send()
            else:
                mailDisco.MailDisco(
                    f"【正常に書き込みました】\nシート: {actual_sheet_name} ({written_pos})\n利用先: {location}\n金額: {money}"
                ).send()

        purpose_instance = purpose.Purpose(location)
        judged_purpose = purpose_instance.judge()

        # 常に4要素[日時, 場所, 金額, 用途]
        data_to_log = [email_time, location, money, judged_purpose]

        all_values = []
        try:
            all_values = worksheet.get_all_values()
        except Exception as e_get_values:
            logger.warning(
                "ワークシートの値の取得に失敗しました: %s。末尾に追加を試みます。",
                e_get_values,
            )
            worksheet.append_row(data_to_log)
            logger.info(
                "ワークシートの値取得に失敗したため、最終行にデータ '%s' が追加されました。",
                data_to_log,
            )
            _notify_write_success("最終行")
            return True

        target_row_index = -1
        if not all_values:  # シートが完全に空の場合
            worksheet.append_row(data_to_log)
            logger.info(
                "シートが空だったため、1行目にデータ '%s' が追加されました。",
                data_to_log,
            )
            _notify_write_success("1行目")
            return True

        # 3行目(index 2)以降でA〜D列がすべて空の行を探すロジック
        target_row_index = find_target_row(all_values)

        if target_row_index is None:  # A〜D列が空の行が見つからなかった場合
            worksheet.append_row(data_to_log)
            logger.info(
                "A〜D列が空の行が見つからなかったため、最終行にデータ '%s' が追加されました。",
                data_to_log,
            )
            _notify_write_success("最終行")
            return True
        else:
            range_to_update = f"A{target_row_index}:D{target_row_index}"
            worksheet.update(values=[data_to_log], range_name=range_to_update)
            logger.info(
                "%d行目 (%s) にデータ '%s' が追加されました。",
                target_row_index,
                range_to_update,
                data_to_log,
            )
            _notify_write_success(f"{target_row_index}行目")
            return True

    except gspread.exceptions.SpreadsheetNotFound:
        # キャッシュが古い可能性があるため破棄し、次回は再取得する
        _cached_spreadsheet = None
        _worksheet_cache = {}
        logger.error(
            "スプレッドシートが見つかりません。IDが正しいか確認してください: %s",
            SPREADSHEET_ID,
        )
        _notify_sheet_failure(
            f"❌ スプレッドシートが見つからず、利用先: {location} / 金額: {money} を記録できませんでした。SPREADSHEET_IDを確認してください。"
        )
        return False
    except Exception as e:
        # シート削除・リネーム等でキャッシュが無効になっている可能性があるため破棄し、次回は再取得する
        _cached_spreadsheet = None
        _worksheet_cache = {}
        logger.error(
            "スプレッドシートへの書き込み中に予期せぬエラーが発生しました: %s",
            e,
            exc_info=True,
        )
        _notify_sheet_failure(
            f"❌ スプレッドシートへの書き込みに失敗しました。\n利用先: {location} / 金額: {money}\nエラー: {e}"
        )
        return False


def timesheet(time):
    """
    利用日時の文字列から、書き込み先のシート名（月）を決定する。

    例: '2023/11/05 12:00' -> '11月'

    :param time: 日時文字列
    :type time: str
    :return: シート名（例: '11月'）
    :rtype: str
    """
    normalized_time = re.sub(r"[\s　]+", " ", str(time).strip()).replace("-", "/")
    time_object = None
    try:
        # まず HH:MM:SS 形式 (デビットカード用など) で試す
        time_object = datetime.datetime.strptime(normalized_time, "%Y/%m/%d %H:%M:%S")
    except ValueError:
        try:
            # 次に HH:MM 形式 (クレジットカード用など) で試す
            time_object = datetime.datetime.strptime(normalized_time, "%Y/%m/%d %H:%M")
        except ValueError:
            # どちらも失敗した場合
            logger.warning(
                "timesheet関数: 日付 '%s' の形式が不正です。デフォルトの月を使用します。",
                time,
            )
            return f"{datetime.datetime.now().month}月"

    return f"{time_object.month}月"


def _is_valid_datetime(text):
    """
    ``YYYY/M/D H:MM`` または ``YYYY/M/D H:MM:SS`` 形式の日時文字列が暦上有効かを判定する。

    :param text: 正規化済みの日時文字列
    :type text: str
    :return: 有効な日時であれば True
    :rtype: bool
    """
    for fmt in ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M"):
        try:
            datetime.datetime.strptime(text, fmt)
            return True
        except ValueError:
            continue
    return False


def parse_mail_body(mes_body):
    """
    メール本文から利用日時・利用先・利用金額を抽出する純粋関数（外部I/Oなし）。

    :param mes_body: 解析対象のメール本文テキスト
    :type mes_body: str
    :return: ``{"datetime": ..., "location": ..., "amount": ...}`` 形式の辞書
    :rtype: dict[str, str | int]
    """
    utilization_datetime_match = re.search(
        r"(?:利用日|ご利用日時)\s*[:：]?\s*(\d{4}[/-]\d{1,2}[/-]\d{1,2}[\s　]+\d{1,2}:\d{2}(?::\d{2})?)",
        mes_body,
    )
    if utilization_datetime_match:
        raw_dt = utilization_datetime_match.group(1)
        utilization_datetime = re.sub(r"[\s　]+", " ", raw_dt.strip()).replace("-", "/")
        if not _is_valid_datetime(utilization_datetime):
            # 13月や25時など暦上ありえない日時は、現在月シートへ誤記録しないよう抽出失敗として扱う
            logger.warning(
                "日時 '%s' が暦上不正なため N/A として扱います。", utilization_datetime
            )
            utilization_datetime = "N/A"
    else:
        utilization_datetime = "N/A"

    utilization_location_match = re.search(
        r"(?:利用先|ご利用場所)[ \t\u3000]*[:：][ \t\u3000]*([^\r\n]*)",
        mes_body,
    )
    raw_location = (
        utilization_location_match.group(1).strip()
        if utilization_location_match
        else ""
    )
    utilization_location = raw_location if raw_location else "N/A"

    utilization_amount_match = re.search(
        r"ご?利用金額\s*[:：]?\s*([-−－▲△]?)\s*([\d,]+)\s*円",
        mes_body,
    )
    utilization_amount = "N/A"
    if utilization_amount_match:
        sign_str, digits_str = (
            utilization_amount_match.group(1),
            utilization_amount_match.group(2),
        )
        if re.fullmatch(r"\d{1,3}(?:,\d{3})*|\d+", digits_str):
            parsed_amount = int(digits_str.replace(",", ""))
            if sign_str in ("-", "−", "－", "▲", "△"):
                parsed_amount = -parsed_amount
            utilization_amount = parsed_amount
        else:
            logger.warning(
                "金額 '%s%s' の書式が不正なため N/A として扱います。",
                sign_str,
                digits_str,
            )

    return {
        "datetime": utilization_datetime,
        "location": utilization_location,
        "amount": utilization_amount,
    }


def find_target_row(all_values, header_rows=2):
    """
    スプレッドシートのデータ行から A〜D 列すべてが空の最初の行番号（1始まり）を返す。

    空き行が見つからない場合は ``None`` を返す。

    :param all_values: ワークシートの全行データ
    :type all_values: list[list]
    :param header_rows: スキップするヘッダ行数（デフォルト: 2）
    :type header_rows: int
    :return: 書き込み先の1始まり行番号、または末尾追記を示す None
    :rtype: int | None
    """
    for i, row in enumerate(all_values):
        if i >= header_rows:
            padded_row = (list(row) + ["", "", "", ""])[:4]
            if not any(str(cell).strip() for cell in padded_row):
                return i + 1
    return None


def _write_token_atomic(token_path, content):
    """
    OAuth トークンファイルを 0600 パーミッションでアトミックに書き込む。

    :param token_path: 保存先ファイルパス
    :type token_path: str
    :param content: 書き込むJSON文字列
    :type content: str
    """
    parent_dir = os.path.dirname(token_path)
    if parent_dir:
        os.makedirs(parent_dir, mode=0o700, exist_ok=True)
        try:
            os.chmod(parent_dir, 0o700)
        except OSError as e:
            # 過去に sudo 実行した等で所有者が異なる場合でも、トークン自体は 0600 で保存して続行する
            logger.warning(
                "ディレクトリ '%s' の権限を 0700 に変更できませんでした: %s",
                parent_dir,
                e,
            )

    tmp_path = f"{token_path}.tmp"
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as token_file:
            token_file.write(content)
            token_file.flush()
            os.fsync(token_file.fileno())
        os.replace(tmp_path, token_path)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def _handle_refresh_error(error):
    """
    Gmail トークン失効時に復旧手順を含むエラーログと通知を送信する（重複スパム抑止）。

    :param error: 送出された RefreshError 例外
    :type error: RefreshError
    """
    global _refresh_error_notified
    logger.error(
        "Gmailトークンが失効しました (%s)。config/gmailtoken.json を削除して再起動し、再認証してください。",
        error,
    )
    if not _refresh_error_notified:
        mailDisco.MailDisco(
            "❌ Gmailの認証トークンが失効しました。\n"
            "config/gmailtoken.json を削除してプログラムを再起動し、再認証してください。"
        ).send()
        _refresh_error_notified = True


def _persist_refreshed_token_if_needed():
    """
    実行中に自動リフレッシュされた Gmail OAuth トークンをディスクへ保存する。
    """
    global _gmail_creds, _last_saved_token_json
    if _gmail_creds is None or not hasattr(_gmail_creds, "to_json"):
        return
    try:
        current_json = _gmail_creds.to_json()
    except Exception:
        return
    if isinstance(current_json, str) and current_json != _last_saved_token_json:
        try:
            _write_token_atomic(TOKEN_FILE, current_json)
            _last_saved_token_json = current_json
            logger.info("更新されたGmailトークンをディスクへ保存しました。")
        except OSError as e:
            logger.error("更新されたGmailトークンの保存に失敗しました: %s", e)


def _reset_refresh_error_state():
    """正常復帰時に RefreshError 通知抑止フラグをリセットする。"""
    global _refresh_error_notified
    _refresh_error_notified = False


def initialize_services():
    """
    Gmail API と Google Sheets API の認証を行い、クライアントを初期化する。

    * Gmail: OAuth2を使用し、トークンがない場合はブラウザ認証を行う。
    * Sheets: サービスアカウントを使用する。

    :return: 両方のサービス初期化に成功した場合 True、失敗した場合 False
    :rtype: bool
    """
    global \
        gc, \
        gmail_service, \
        _cached_gc, \
        _cached_spreadsheet_id, \
        _cached_spreadsheet, \
        _worksheet_cache, \
        _gmail_creds, \
        _last_saved_token_json, \
        _refresh_error_notified

    logger.info("--- サービス初期化開始 ---")

    # Gmail認証
    gmail_creds = None
    if os.path.exists(TOKEN_FILE):
        try:
            gmail_creds = GmailCredentials.from_authorized_user_file(
                TOKEN_FILE, [SCOPES_GMAIL]
            )
        except (ValueError, OSError) as e:
            logger.warning(
                "保存済みGmailトークン '%s' の読み込みに失敗したため再認証を行います: %s",
                TOKEN_FILE,
                e,
            )
            gmail_creds = None

    if not gmail_creds or not gmail_creds.valid:
        if gmail_creds and gmail_creds.expired and gmail_creds.refresh_token:
            try:
                gmail_creds.refresh(Request())
                logger.info("Gmailトークンをリフレッシュしました。")
            except Exception as e:
                logger.error("Gmailトークンのリフレッシュに失敗: %s", e, exc_info=True)
                logger.error(
                    "'credentials.json' を確認し、 'gmailtoken.json' を削除して再認証してください。"
                )
                if not _refresh_error_notified:
                    mailDisco.MailDisco(
                        "❌ Gmailトークンの更新に失敗しました。\n"
                        "config/gmailtoken.json を削除してプログラムを再起動し、再認証してください。"
                    ).send()
                    _refresh_error_notified = True
                return False
        else:
            try:
                credentials_path = os.path.join(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "config",
                    "credentials.json",
                )
                flow = InstalledAppFlow.from_client_secrets_file(
                    credentials_path, [SCOPES_GMAIL]
                )
                gmail_creds = flow.run_local_server(port=0)
            except FileNotFoundError:
                logger.error("'config/credentials.json' が見つかりません。")
                return False

        token_json = gmail_creds.to_json()
        try:
            _write_token_atomic(TOKEN_FILE, token_json)
            _last_saved_token_json = token_json
        except OSError as e:
            # 所有者違い等で保存できなくてもメモリ上のトークンで稼働を続ける（次回トークン更新時に再保存を試みる）
            logger.error("Gmailトークンの保存に失敗しました: %s", e)

    try:
        gmail_service = build("gmail", "v1", credentials=gmail_creds)
        _gmail_creds = gmail_creds
        if _last_saved_token_json is None and hasattr(gmail_creds, "to_json"):
            try:
                initial_json = gmail_creds.to_json()
                if isinstance(initial_json, str):
                    _last_saved_token_json = initial_json
            except Exception:
                pass
        _refresh_error_notified = False
        logger.info("Gmail APIサービスに接続しました。")
    except HttpError as error:
        logger.error(
            "Gmail APIサービスへの接続中にエラーが発生しました: %s",
            error,
            exc_info=True,
        )
        return False  # 初期化失敗

    # GSpread認証と必須設定の検証
    try:
        if not SERVICE_ACCOUNT_FILE:
            logger.error(
                "環境変数 'SERVICE_ACCOUNT_FILE' が設定されていません。 .envファイルを確認してください。"
            )
            return False
        if not SPREADSHEET_ID:
            logger.error(
                "環境変数 'SPREADSHEET_ID' が設定されていません。 .envファイルを確認してください。"
            )
            return False
        if not os.getenv("DISCORD_WEBHOOK_URL"):
            logger.warning(
                "環境変数 'DISCORD_WEBHOOK_URL' が設定されていません。Discord通知は無効の状態で稼働します。"
            )

        gc = gspread.service_account(
            filename=SERVICE_ACCOUNT_FILE,
            scopes=SCOPES_SHEET,
            http_client=BoundedBackOffHTTPClient,
        )
        spreadsheet = gc.open_by_key(SPREADSHEET_ID)
        _cached_gc = gc
        _cached_spreadsheet_id = SPREADSHEET_ID
        _cached_spreadsheet = spreadsheet
        _worksheet_cache = {}
        logger.info("Google スプレッドシートAPIに接続しました。")
    except FileNotFoundError:
        logger.error(
            "サービスアカウントファイル '%s' が見つかりません。 (.envファイルを確認してください)",
            SERVICE_ACCOUNT_FILE,
        )
        return False
    except gspread.exceptions.SpreadsheetNotFound:
        logger.error(
            "スプレッドシートが見つかりません。SPREADSHEET_ID が正しいか確認してください。"
        )
        mailDisco.MailDisco(
            "❌ 起動エラー: スプレッドシートが見つかりません。SPREADSHEET_ID と共有設定を確認してください。"
        ).send()
        return False
    except Exception as e:
        logger.error("Google スプレッドシートAPIの認証エラー: %s", e, exc_info=True)
        logger.error(
            "サービスアカウントのメールアドレスをスプレッドシートに共有（編集者権限）したか確認してください。"
        )
        return False  # 初期化失敗

    logger.info("--- サービス初期化完了 ---")
    return True  # 初期化成功


def check_mail_job():
    """
    Gmailを巡回し、新規の対象メール（三井住友カード利用通知など）を処理する定期実行ジョブ。

    1. 前回実行日時以降のメール、または未処理のメールを検索する。
    2. 件名が一致するメールのIDを取得し、処理済みリスト（processed_ids）と比較する。
    3. 未処理の場合、メール本文を取得して ``system`` 関数に渡す。
    4. 処理完了後、実行日時と処理済みIDを保存する。

    :return: なし
    :rtype: None
    """
    global gc, gmail_service, processed_ids, _sheet_failure_notify_enabled

    if gmail_service is None or gc is None:
        logger.error("サービスが初期化されていません。ジョブを実行できません。")
        return

    saved_in_loop = False

    logger.info(
        "--- %s: 定時メールチェック実行 ---",
        datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )

    current_check_time = datetime.datetime.now(datetime.timezone.utc)
    last_check_time = get_last_run_time()

    # Gmail検索クエリ（アーカイブ済みも含め、ゴミ箱・迷惑メール・送信済み・下書きは除外）。
    # 送信済み・下書きを除外しないと、通知メールの転送（Fwd:）や返信の引用本文が二重記録される
    query = GMAIL_QUERY_BASE
    # 検索範囲は処理済みIDの保持期間内に限定する。last_run_time の欠損や長期凍結時に、
    # 保持期間を過ぎてIDがパージされた過去メールを再取得して二重記録しないため
    earliest_timestamp = int(
        (current_check_time - datetime.timedelta(days=SEARCH_WINDOW_DAYS)).timestamp()
    )
    if last_check_time:
        query_timestamp = max(
            earliest_timestamp,
            int(last_check_time.timestamp()) - LAST_RUN_OVERLAP_SECONDS,
        )
        if query_timestamp == earliest_timestamp:
            logger.warning(
                "最終チェック日時 (%s) が検索範囲の上限 (%d日) より古いため、直近 %d 日分のみ検索します。",
                last_check_time.isoformat(),
                SEARCH_WINDOW_DAYS,
                SEARCH_WINDOW_DAYS,
            )
        else:
            logger.info(
                "最終チェック日時 (%s) 以降を検索 (after:%d)。",
                last_check_time.isoformat(),
                query_timestamp,
            )
    else:
        query_timestamp = earliest_timestamp
        logger.info(
            "初回チェックまたは日時不明。直近 %d 日分の対象メールを検索候補とします。",
            SEARCH_WINDOW_DAYS,
        )
    query += f" after:{query_timestamp}"

    try:
        messages = []
        page_token = None
        while True:
            list_kwargs = {"userId": "me", "q": query, "maxResults": 500}
            if page_token:
                list_kwargs["pageToken"] = page_token
            results = (
                gmail_service.users()
                .messages()
                .list(**list_kwargs)
                .execute(num_retries=3)
            )
            messages.extend(results.get("messages", []))
            page_token = results.get("nextPageToken")
            if not page_token:
                break

        had_failure = False
        interrupted = False

        if not messages:
            logger.info("新しい対象メールは見つかりませんでした。")
        else:
            new_messages_processed_count = 0
            logger.info(
                "%d 件の候補メールが見つかりました。処理を開始します。", len(messages)
            )

            # Gmail API は新着順(降順)で返すため、古い順(昇順)に処理してスプレッドシートの時系列を保つ
            for message_info in reversed(messages):
                if _shutdown_requested:
                    # 1通単位の処理（書き込み→処理済み登録）を終えた区切りで停止し、重複記録を防ぐ
                    interrupted = True
                    logger.info(
                        "停止要求を受信したため、残りのメール処理を中断します。"
                    )
                    break

                message_id = message_info["id"]

                if message_id in processed_ids:
                    continue

                logger.info("処理中のメールID: %s", message_id)
                subject = "取得前"
                failure_reason = None
                notify_first = True
                try:
                    msg_detail = (
                        gmail_service.users()
                        .messages()
                        .get(userId="me", id=message_id, format="full")
                        .execute(num_retries=3)
                    )

                    headers = msg_detail["payload"]["headers"]
                    subject = "件名なし"
                    for header in headers:
                        if header["name"].lower() == "subject":
                            subject = header["value"]
                            break

                    logger.info("件名: %s", subject)

                    body = get_email_body(msg_detail["payload"])
                    if body:
                        # 書き込み失敗の詳細通知は初回の試行時のみ送る
                        _sheet_failure_notify_enabled = (
                            message_id not in _message_failure_counts
                        )
                        try:
                            result = system(body)
                        finally:
                            _sheet_failure_notify_enabled = True
                        if result == SHEET_SKIPPED:
                            # 抽出失敗は再試行しても解消しないため処理済みにし、永久リトライを防ぐ（通知は sheet() 内で送信済み）
                            processed_ids.add(message_id)
                            save_processed_ids()
                            saved_in_loop = True
                            logger.warning(
                                "メールID %s は抽出失敗のため記録をスキップし、処理済みとしてマークしました。",
                                message_id,
                            )
                        elif result:
                            processed_ids.add(message_id)
                            save_processed_ids()
                            saved_in_loop = True
                            new_messages_processed_count += 1
                            logger.info(
                                "メールID %s の処理が完了し、処理済みとしてマークしました。",
                                message_id,
                            )
                        else:
                            # 失敗内容（利用先・金額・エラー）は sheet() 内で通知済みのため、ここでは重複通知しない
                            failure_reason = (
                                "スプレッドシートへの書き込みに失敗しました"
                            )
                            notify_first = False
                    else:
                        failure_reason = "本文を取得できませんでした"

                except RefreshError:
                    raise
                except HttpError as e_msg_get:
                    logger.error(
                        "メールID %s の詳細取得中にエラー: %s", message_id, e_msg_get
                    )
                    failure_reason = (
                        f"メールの取得中にエラーが発生しました ({e_msg_get})"
                    )
                except Exception as e_proc:
                    logger.error(
                        "メールID %s の処理中に予期せぬエラー: %s",
                        message_id,
                        e_proc,
                        exc_info=True,
                    )
                    failure_reason = f"処理中に予期せぬエラーが発生しました ({e_proc})"

                # 失敗回数は変更のたびに保存し、後続メールで RefreshError 等により中断されても失われないようにする
                if failure_reason is None:
                    if _message_failure_counts.pop(message_id, None) is not None:
                        save_message_failure_counts()
                else:
                    gave_up = _record_message_failure(
                        message_id, subject, failure_reason, notify_first=notify_first
                    )
                    save_message_failure_counts()
                    if gave_up:
                        saved_in_loop = True
                    else:
                        had_failure = True

            if new_messages_processed_count > 0:
                logger.info(
                    "%d 件の新しいメールの処理が完了しました。",
                    new_messages_processed_count,
                )
            else:
                logger.info("新しい未処理メールはありませんでした（処理済みを除く）。")

        if had_failure:
            logger.warning(
                "処理に失敗したメールがあるため、last_run_time を更新せず次回再試行します。"
            )
        elif interrupted:
            logger.warning(
                "処理を中断したため、last_run_time を更新せず次回再開します。"
            )
        else:
            save_current_run_time(current_check_time)
        if not saved_in_loop:
            save_processed_ids()
        _persist_refreshed_token_if_needed()
        _reset_refresh_error_state()

    except RefreshError as error:
        _handle_refresh_error(error)
    except HttpError as error:
        logger.error(
            "Gmail API呼び出し中にエラーが発生しました: %s", error, exc_info=True
        )
        mailDisco.MailDisco("❌ Gmail API呼び出し中にエラーが発生しました").send()
        _persist_refreshed_token_if_needed()
    except Exception as e:
        logger.error("ジョブ実行で予期せぬエラーが発生しました: %s", e, exc_info=True)
        mailDisco.MailDisco("ジョブ実行で予期せぬエラーが発生しました").send()
        _persist_refreshed_token_if_needed()

    logger.info(
        "--- %s: メールチェック完了 ---",
        datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )


# --- メインの実行ブロック ---
def _handle_sigterm(_signum, _frame):
    """
    SIGTERM 受信時に停止要求フラグを立てる。

    シグナルハンドラ内でファイル保存・ログ出力・HTTP 通信を行うと、割り込まれた処理
    （シート書き込み直後の処理済み登録、``.tmp`` 経由の状態保存、共有 Session）と競合するため、
    実際の状態保存・停止通知はメインループ側の :func:`_shutdown` で行う。
    """
    global _shutdown_requested
    _shutdown_requested = True


def _shutdown():
    """状態を保存し、停止通知を送る（systemd の停止タイムアウトを超えないよう 429 は再試行しない）。"""
    save_processed_ids()
    save_message_failure_counts()
    mailDisco.MailDisco(
        "--- メール監視システムを停止しました ---", max_retries=0
    ).send()


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _handle_sigterm)

    load_processed_ids()  # 最初に処理済みIDをロード
    load_message_failure_counts()

    if initialize_services():  # サービス初期化
        logger.info("サービス初期化完了。スケジューラーを開始します。")
        mailDisco.MailDisco("--- メール監視システムを起動しました ---").send()

        # スケジュールの設定
        schedule.every().hour.at(":00").do(check_mail_job)
        schedule.every().hour.at(":30").do(check_mail_job)

        logger.info("スケジューラーを実行中です。")
        logger.info(
            "次回の実行は %s です。", schedule.next_run().strftime("%Y-%m-%d %H:%M:%S")
        )

        try:
            # 起動時にすぐ実行したい場合は、以下のコメントを解除
            logger.info("初回実行中...")
            check_mail_job()
            time.sleep(1)

            logger.info("初回実行完了。")

            while not _shutdown_requested:
                schedule.run_pending()
                time.sleep(1)
            logger.info("SIGTERMを受信したため、状態を保存してプログラムを停止します。")
        except KeyboardInterrupt:
            logger.info("プログラムを停止しました。")
        _shutdown()

    else:
        logger.error("サービスの初期化に失敗したため、プログラムを終了します。")
        mailDisco.MailDisco(
            "❌ サービスの初期化に失敗しました。プログラムを終了します。"
        ).send()
        # systemd の Restart=on-failure で自動再起動されるよう、異常終了コードで終了する
        raise SystemExit(1)
