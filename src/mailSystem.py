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
SCOPES_SHEET = ['https://www.googleapis.com/auth/spreadsheets']
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")

# --- グローバル変数 ---
gc = None
processed_ids = set()
gmail_service = None
_cached_gc = None
_cached_spreadsheet_id = None
_cached_spreadsheet = None
_worksheet_cache = {}

# sheet() の戻り値: 抽出失敗(N/A)のため記録をスキップした（再試行しても結果が変わらないため処理済み扱いにする）
SHEET_SKIPPED = "skipped"


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
            match = re.search(r'charset\s*=\s*"?([^";\s]+)"?', header.get("value", ""), re.IGNORECASE)
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
    decoded_bytes = base64.urlsafe_b64decode(data + '=' * ((-len(data)) % 4))

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

    if last_error is not None:
        raise last_error
    raise UnicodeDecodeError("utf-8", decoded_bytes, 0, len(decoded_bytes), "failed to decode email body")


def _html_to_text(html_body):
    """
    HTML文字列からスクリプト・スタイル・タグを除去し、テキストへ変換する。

    :param html_body: HTML本文
    :type html_body: str
    :return: プレーンテキスト化された文字列
    :rtype: str
    """
    text = re.sub(r"<style\b[^>]*>.*?</style>", "", html_body, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<script\b[^>]*>.*?</script>", "", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<br\s*/?>|</tr>|</p>|</div>|</li>", "\n", text, flags=re.IGNORECASE)
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

    data = payload.get("body", {}).get("data")
    if not data:
        return "", ""

    mime_type = (payload.get("mimeType") or "").lower()
    if mime_type and not mime_type.startswith("text/"):
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

    logger.warning("本文パートを抽出できませんでした。MIME構造: %s", _summarize_mime_tree(msg_payload))
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


def get_last_run_time():
    """
    前回の実行日時をJSONファイルから読み込む。

    :return: 前回実行日時。ファイルが存在しないか破損している場合は None
    :rtype: datetime.datetime | None
    """
    if os.path.exists(LAST_RUN_FILE):
        try:
            with open(LAST_RUN_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return datetime.datetime.fromisoformat(data.get("last_run_time"))
        except (json.JSONDecodeError, TypeError, ValueError, OSError) as e:
            logger.warning("前回の実行日時ファイルが不正です (%s)。新しく作成します。", e)
            mailDisco.MailDisco(
                "⚠️ 前回の実行日時ファイルが破損または読み込み不能のためリセットしました。"
            ).send()
            return None
    return None

def save_current_run_time(current_time):
    """
    現在の実行日時をJSONファイルに保存する。

    :param current_time: 保存する日時オブジェクト
    :type current_time: datetime.datetime
    """
    try:
        _atomic_write_json(LAST_RUN_FILE, {"last_run_time": current_time.isoformat()})
    except OSError as e:
        logger.error("前回実行日時の保存中にエラーが発生しました: %s", e)

def load_processed_ids():
    """
    処理済みのメールIDリストをファイルから読み込み、グローバル変数 ``processed_ids`` を更新する。
    重複処理を防ぐために使用される。
    """
    global processed_ids
    if os.path.exists(PROCESSED_IDS_FILE):
        try:
            with open(PROCESSED_IDS_FILE, "r", encoding="utf-8") as f:
                ids_list = json.load(f)
                processed_ids = set(ids_list)
                logger.info("処理済みメールIDを %d 件ロードしました。", len(processed_ids))
        except (json.JSONDecodeError, TypeError, OSError) as e:
            logger.warning("処理済みメールIDファイル '%s' が不正です。新しく作成します。エラー: %s", PROCESSED_IDS_FILE, e)
            mailDisco.MailDisco(
                "⚠️ 処理済みメールIDファイルが破損または読み込み不能のため初期化しました。重複記録に注意してください。"
            ).send()
            processed_ids = set()
    else:
        logger.info("処理済みメールIDファイル '%s' が見つかりません。新しく作成します。", PROCESSED_IDS_FILE)
        processed_ids = set()

def save_processed_ids():
    """
    現在の処理済みメールIDセット（``processed_ids``）をJSONファイルに保存する。
    """
    global processed_ids
    try:
        _atomic_write_json(PROCESSED_IDS_FILE, list(processed_ids))
    except OSError as e:
        logger.error("処理済みメールIDの保存中にエラーが発生しました: %s", e)


def system(mes_body):
    """
    メール本文を正規表現で解析し、主要な家計簿データ（日時、場所、金額）を抽出する。

    抽出後は ``sheet`` 関数を呼び出してスプレッドシートへの記録を行う。

    :param mes_body: 解析対象のメール本文テキスト
    :type mes_body: str
    :return: 書き込み成功時 True、失敗時 False、抽出失敗でスキップした場合 ``SHEET_SKIPPED``
    :rtype: bool | str
    """
    utilization_datetime_match = re.search(
        r"(?:利用日|ご利用日時)\s*[:：]?\s*(\d{4}[/-]\d{1,2}[/-]\d{1,2}[\s　]+\d{1,2}:\d{2}(?::\d{2})?)",
        mes_body,
    )
    if utilization_datetime_match:
        raw_dt = utilization_datetime_match.group(1)
        utilization_datetime = re.sub(r"[\s　]+", " ", raw_dt.strip()).replace("-", "/")
    else:
        utilization_datetime = "N/A"
    utilization_location_match = re.search(r"(?:利用先|ご利用場所)\s*[:：]\s*([^\n]+)", mes_body)
    utilization_location = utilization_location_match.group(1).strip() if utilization_location_match else "N/A"
    utilization_amount_match = re.search(
        r"ご?利用金額\s*[:：]?\s*([-−－▲△]?)\s*([\d,]+)\s*円",
        mes_body,
    )
    utilization_amount = "N/A"
    if utilization_amount_match:
        sign_str, digits_str = utilization_amount_match.group(1), utilization_amount_match.group(2)
        if re.fullmatch(r"\d{1,3}(?:,\d{3})*|\d+", digits_str):
            parsed_amount = int(digits_str.replace(",", ""))
            if sign_str in ("-", "−", "－", "▲", "△"):
                parsed_amount = -parsed_amount
            utilization_amount = parsed_amount
        else:
            logger.warning("金額 '%s%s' の書式が不正なため N/A として扱います。", sign_str, digits_str)
    
    logger.info("抽出結果: 利用日=%s, 利用先=%s, 利用金額=%s", utilization_datetime, utilization_location, utilization_amount)
    
    return sheet(utilization_datetime, utilization_location, utilization_amount)


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

        is_complete_data = True
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
                logger.error("ワークシート 'Sheet1' も見つかりません。書き込みを中止します。")
                mailDisco.MailDisco(
                    f"❌ ワークシート '{sheet_name}' も 'Sheet1' も見つからず、利用先: {location} / 金額: {money} を記録できませんでした。"
                ).send()
                return False

        def _notify_write_success(written_pos):
            if used_fallback_sheet:
                mailDisco.MailDisco(
                    f"⚠️ ワークシート '{sheet_name}' が見つからず、'Sheet1' ({written_pos}) に書き込みました。\n利用先: {location} / 金額: {money}"
                ).send()
            elif is_complete_data:
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
            logger.warning("ワークシートの値の取得に失敗しました: %s。末尾に追加を試みます。", e_get_values)
            worksheet.append_row(data_to_log)
            logger.info("ワークシートの値取得に失敗したため、最終行にデータ '%s' が追加されました。", data_to_log)
            _notify_write_success("最終行")
            return True

        target_row_index = -1
        if not all_values: # シートが完全に空の場合
            worksheet.append_row(data_to_log)
            logger.info("シートが空だったため、1行目にデータ '%s' が追加されました。", data_to_log)
            _notify_write_success("1行目")
            return True
        
        # 3行目(index 2)以降でA〜D列がすべて空の行を探すロジック
        for i, row in enumerate(all_values):
            if i >= 2:
                padded_row = (list(row) + ["", "", "", ""])[:4]
                if not any(str(cell).strip() for cell in padded_row):
                    target_row_index = i + 1
                    break
        
        if target_row_index == -1: # A〜D列が空の行が見つからなかった場合
            worksheet.append_row(data_to_log)
            logger.info("A〜D列が空の行が見つからなかったため、最終行にデータ '%s' が追加されました。", data_to_log)
            _notify_write_success("最終行")
            return True
        else:
            range_to_update = f"A{target_row_index}:D{target_row_index}"
            worksheet.update(values=[data_to_log], range_name=range_to_update)
            logger.info("%d行目 (%s) にデータ '%s' が追加されました。", target_row_index, range_to_update, data_to_log)
            _notify_write_success(f"{target_row_index}行目")
            return True

    except gspread.exceptions.SpreadsheetNotFound:
        # キャッシュが古い可能性があるため破棄し、次回は再取得する
        _cached_spreadsheet = None
        _worksheet_cache = {}
        logger.error("スプレッドシートが見つかりません。IDが正しいか確認してください: %s", SPREADSHEET_ID)
        mailDisco.MailDisco(
            f"❌ スプレッドシートが見つからず、利用先: {location} / 金額: {money} を記録できませんでした。SPREADSHEET_IDを確認してください。"
        ).send()
        return False
    except Exception as e:
        # シート削除・リネーム等でキャッシュが無効になっている可能性があるため破棄し、次回は再取得する
        _cached_spreadsheet = None
        _worksheet_cache = {}
        logger.error("スプレッドシートへの書き込み中に予期せぬエラーが発生しました: %s", e, exc_info=True)
        mailDisco.MailDisco(
            f"❌ スプレッドシートへの書き込みに失敗しました。\n利用先: {location} / 金額: {money}\nエラー: {e}"
        ).send()
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
        time_object = datetime.datetime.strptime(normalized_time, '%Y/%m/%d %H:%M:%S')
    except ValueError:
        try:
            # 次に HH:MM 形式 (クレジットカード用など) で試す
            time_object = datetime.datetime.strptime(normalized_time, '%Y/%m/%d %H:%M')
        except ValueError:
            # どちらも失敗した場合
            logger.warning("timesheet関数: 日付 '%s' の形式が不正です。デフォルトの月を使用します。", time)
            return f'{datetime.datetime.now().month}月'
    
    return f'{time_object.month}月'


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
        os.chmod(parent_dir, 0o700)

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


def initialize_services():
    """
    Gmail API と Google Sheets API の認証を行い、クライアントを初期化する。

    * Gmail: OAuth2を使用し、トークンがない場合はブラウザ認証を行う。
    * Sheets: サービスアカウントを使用する。

    :return: 両方のサービス初期化に成功した場合 True、失敗した場合 False
    :rtype: bool
    """
    global gc, gmail_service, _cached_gc, _cached_spreadsheet_id, _cached_spreadsheet, _worksheet_cache

    logger.info("--- サービス初期化開始 ---")
    
    # Gmail認証
    gmail_creds = None
    if os.path.exists(TOKEN_FILE):
        try:
            gmail_creds = GmailCredentials.from_authorized_user_file(TOKEN_FILE, [SCOPES_GMAIL])
        except (ValueError, OSError) as e:
            logger.warning("保存済みGmailトークン '%s' の読み込みに失敗したため再認証を行います: %s", TOKEN_FILE, e)
            gmail_creds = None
    
    if not gmail_creds or not gmail_creds.valid:
        if gmail_creds and gmail_creds.expired and gmail_creds.refresh_token:
            try:
                gmail_creds.refresh(Request())
                logger.info("Gmailトークンをリフレッシュしました。")
            except Exception as e:
                logger.error("Gmailトークンのリフレッシュに失敗: %s", e, exc_info=True)
                logger.error("'credentials.json' を確認し、 'gmailtoken.json' を削除して再認証してください。")
                return False
        else:
            try:
                credentials_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "credentials.json")
                flow = InstalledAppFlow.from_client_secrets_file(
                    credentials_path, [SCOPES_GMAIL]
                )
                gmail_creds = flow.run_local_server(port=0)
            except FileNotFoundError:
                logger.error("'config/credentials.json' が見つかりません。")
                return False
        
        _write_token_atomic(TOKEN_FILE, gmail_creds.to_json())

    try:
        gmail_service = build("gmail", "v1", credentials=gmail_creds)
        logger.info("Gmail APIサービスに接続しました。")
    except HttpError as error:
        logger.error("Gmail APIサービスへの接続中にエラーが発生しました: %s", error, exc_info=True)
        return False # 初期化失敗

    # GSpread認証と必須設定の検証
    try:
        if not SERVICE_ACCOUNT_FILE:
            logger.error("環境変数 'SERVICE_ACCOUNT_FILE' が設定されていません。 .envファイルを確認してください。")
            return False
        if not SPREADSHEET_ID:
            logger.error("環境変数 'SPREADSHEET_ID' が設定されていません。 .envファイルを確認してください。")
            return False
        if not os.getenv("DISCORD_WEBHOOK_URL"):
            logger.warning("環境変数 'DISCORD_WEBHOOK_URL' が設定されていません。Discord通知は無効の状態で稼働します。")

        sa_kwargs = {"filename": SERVICE_ACCOUNT_FILE, "scopes": SCOPES_SHEET}
        backoff_client = getattr(gspread, "BackOffHTTPClient", None)
        if backoff_client is not None:
            sa_kwargs["http_client"] = backoff_client
        gc = gspread.service_account(**sa_kwargs)
        spreadsheet = gc.open_by_key(SPREADSHEET_ID)
        _cached_gc = gc
        _cached_spreadsheet_id = SPREADSHEET_ID
        _cached_spreadsheet = spreadsheet
        _worksheet_cache = {}
        logger.info("Google スプレッドシートAPIに接続しました。")
    except FileNotFoundError:
        logger.error("サービスアカウントファイル '%s' が見つかりません。 (.envファイルを確認してください)", SERVICE_ACCOUNT_FILE)
        return False
    except gspread.exceptions.SpreadsheetNotFound:
        logger.error("スプレッドシートが見つかりません。SPREADSHEET_ID が正しいか確認してください。")
        mailDisco.MailDisco(
            "❌ 起動エラー: スプレッドシートが見つかりません。SPREADSHEET_ID と共有設定を確認してください。"
        ).send()
        return False
    except Exception as e:
        logger.error("Google スプレッドシートAPIの認証エラー: %s", e, exc_info=True)
        logger.error("サービスアカウントのメールアドレスをスプレッドシートに共有（編集者権限）したか確認してください。")
        return False # 初期化失敗
    
    logger.info("--- サービス初期化完了 ---")
    return True # 初期化成功

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
    global gc, gmail_service, processed_ids

    if gmail_service is None or gc is None:
        logger.error("サービスが初期化されていません。ジョブを実行できません。")
        return

    saved_in_loop = False

    logger.info("--- %s: 定時メールチェック実行 ---", datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    
    current_check_time = datetime.datetime.now()
    last_check_time = get_last_run_time()

    # Gmail検索クエリに対象の件名を追加（アーカイブ済みも含め、ゴミ箱・迷惑メールは除外）
    query = 'subject:"ご利用のお知らせ【三井住友カード】" -in:trash -in:spam'
    if last_check_time:
        timestamp = int(last_check_time.timestamp())
        query += f" after:{timestamp}"
        logger.info("最終チェック日時 (%s) 以降を検索。", last_check_time.strftime('%Y-%m-%d %H:%M:%S'))
    else:
        logger.info("初回チェックまたは日時不明。対象件名の全メールを検索候補とします。")

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

        if not messages:
            logger.info("新しい対象メールは見つかりませんでした。")
        else:
            new_messages_processed_count = 0
            logger.info("%d 件の候補メールが見つかりました。処理を開始します。", len(messages))

            # Gmail API は新着順(降順)で返すため、古い順(昇順)に処理してスプレッドシートの時系列を保つ
            for message_info in reversed(messages):
                message_id = message_info["id"]
                
                if message_id in processed_ids:
                    continue
                
                logger.info("処理中のメールID: %s", message_id)
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
                        result = system(body)
                        if result == SHEET_SKIPPED:
                            # 抽出失敗は再試行しても解消しないため処理済みにし、永久リトライを防ぐ（通知は sheet() 内で送信済み）
                            processed_ids.add(message_id)
                            save_processed_ids()
                            saved_in_loop = True
                            logger.warning("メールID %s は抽出失敗のため記録をスキップし、処理済みとしてマークしました。", message_id)
                        elif result:
                            processed_ids.add(message_id)
                            save_processed_ids()
                            saved_in_loop = True
                            new_messages_processed_count += 1
                            logger.info("メールID %s の処理が完了し、処理済みとしてマークしました。", message_id)
                        else:
                            had_failure = True
                            logger.error("メールID %s のスプレッドシート書き込みに失敗したため、処理済みに登録しません。", message_id)
                            mailDisco.MailDisco(
                                f"❌ メールID {message_id} のスプレッドシート書き込みに失敗しました。処理済みには登録せずスキップします。"
                            ).send()
                    else:
                        had_failure = True
                        logger.warning("メールID %s の本文が見つかりませんでした。処理済みに登録せずスキップします。", message_id)
                        mailDisco.MailDisco(
                            f"⚠️ メールID {message_id} の本文を取得できなかったためスキップしました（処理済み未登録）。"
                        ).send()
                
                except HttpError as e_msg_get:
                    had_failure = True
                    logger.error("メールID %s の詳細取得中にエラー: %s", message_id, e_msg_get)
                    mailDisco.MailDisco(
                        f"❌ メールID {message_id} の取得中にエラーが発生しました。次回再試行するため最終チェック時刻を更新しません。"
                    ).send()
                except Exception as e_proc:
                    had_failure = True
                    logger.error("メールID %s の処理中に予期せぬエラー: %s", message_id, e_proc, exc_info=True)
                    mailDisco.MailDisco(
                        f"❌ メールID {message_id} の処理中に予期せぬエラーが発生しました。次回再試行するため最終チェック時刻を更新しません。"
                    ).send()

            if new_messages_processed_count > 0:
                 logger.info("%d 件の新しいメールの処理が完了しました。", new_messages_processed_count)
            else:
                 logger.info("新しい未処理メールはありませんでした（処理済みを除く）。")
        
        if not had_failure:
            save_current_run_time(current_check_time)
        else:
            logger.warning("処理に失敗したメールがあるため、last_run_time を更新せず次回再試行します。")
        if not saved_in_loop:
            save_processed_ids()

    except HttpError as error:
        logger.error("Gmail API呼び出し中にエラーが発生しました: %s", error, exc_info=True)
        mailDisco.MailDisco("❌ Gmail API呼び出し中にエラーが発生しました").send()
    except Exception as e:
        logger.error("ジョブ実行で予期せぬエラーが発生しました: %s", e, exc_info=True)
        mailDisco.MailDisco("ジョブ実行で予期せぬエラーが発生しました").send()

    logger.info("--- %s: メールチェック完了 ---", datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'))




# --- メインの実行ブロック ---
def _handle_sigterm(_signum, _frame):
    """SIGTERM 受信時に処理済みIDを保存し、停止通知を送って終了する。"""
    logger.info("SIGTERMを受信しました。状態を保存してプログラムを停止します。")
    save_processed_ids()
    mailDisco.MailDisco("--- メール監視システムを停止しました ---").send()
    raise SystemExit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _handle_sigterm)

    load_processed_ids() # 最初に処理済みIDをロード

    if initialize_services(): # サービス初期化
        logger.info("サービス初期化完了。スケジューラーを開始します。")
        mailDisco.MailDisco("--- メール監視システムを起動しました ---").send()

        # スケジュールの設定
        schedule.every().hour.at(":00").do(check_mail_job)
        schedule.every().hour.at(":30").do(check_mail_job)

        logger.info("スケジューラーを実行中です。")
        logger.info("次回の実行は %s です。", schedule.next_run().strftime('%Y-%m-%d %H:%M:%S'))

        try:
            # 起動時にすぐ実行したい場合は、以下のコメントを解除
            logger.info("初回実行中...")
            check_mail_job()
            time.sleep(1)

            logger.info("初回実行完了。")

            while True:
                schedule.run_pending()
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("プログラムを停止しました。")
            save_processed_ids()
            mailDisco.MailDisco("--- メール監視システムを停止しました ---").send()

    else:
        logger.error("サービスの初期化に失敗したため、プログラムを終了します。")
        mailDisco.MailDisco("❌ サービスの初期化に失敗しました。プログラムを終了します。").send()
