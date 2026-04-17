import os.path
import time
import datetime
import json
import base64
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
SERVICE_ACCOUNT_FILE = os.getenv("SERVICE_ACCOUNT_FILE")
SCOPES_SHEET = ['https://www.googleapis.com/auth/spreadsheets',
                'https://www.googleapis.com/auth/drive']
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")

# --- グローバル変数 ---
gc = None
processed_ids = set()
gmail_service = None


def decode_base64_url_safe(data):
    """
    URLセーフなBase64エンコードデータをデコードする。

    パディング（=）の調整を行い、UTF-8でのデコードを試みる。
    失敗した場合は iso-2022-jp（日本のメールで一般的）でのデコードを試行する。

    :param data: Base64エンコードされた文字列
    :type data: str
    :return: デコードされた文字列
    :rtype: str
    """
    decoded_bytes = base64.urlsafe_b64decode(data + '=' * ((-len(data)) % 4))
    try:
        # まずUTF-8で実行
        return decoded_bytes.decode('utf-8')
    except UnicodeDecodeError:
        # UTF-8で失敗した場合、iso-2022-jpで実行
        return decoded_bytes.decode('iso-2022-jp')

def get_email_body(msg_payload):
    """
    Gmail APIのペイロード構造からメール本文（プレーンテキストまたはHTML）を抽出・デコードする。

    :param msg_payload: Gmail APIから取得したメッセージのペイロード辞書
    :type msg_payload: dict
    :return: デコードされたメール本文。抽出できない場合は空文字を返す。
    :rtype: str
    """
    body_data = ""
    if "parts" in msg_payload:
        for part in msg_payload["parts"]:
            mime_type = part.get("mimeType")
            if mime_type == "text/plain":
                if "body" in part and "data" in part["body"]:
                    return decode_base64_url_safe(part["body"]["data"])
            elif mime_type == "text/html":
                if "body" in part and "data" in part["body"]:
                    html_body = decode_base64_url_safe(part["body"]["data"])
                    body_data = html_body
    elif "body" in msg_payload and "data" in msg_payload["body"]:
        body_data = decode_base64_url_safe(msg_payload["body"]["data"])
    return body_data

def get_last_run_time():
    """
    前回の実行日時をJSONファイルから読み込む。

    :return: 前回実行日時。ファイルが存在しないか破損している場合は None
    :rtype: datetime.datetime | None
    """
    if os.path.exists(LAST_RUN_FILE):
        with open(LAST_RUN_FILE, "r") as f:
            try:
                data = json.load(f)
                return datetime.datetime.fromisoformat(data.get("last_run_time"))
            except (json.JSONDecodeError, TypeError, ValueError):
                logger.warning("前回の実行日時ファイルが不正です。新しく作成します。")
                return None
    return None

def save_current_run_time(current_time):
    """
    現在の実行日時をJSONファイルに保存する。

    :param current_time: 保存する日時オブジェクト
    :type current_time: datetime.datetime
    """
    with open(LAST_RUN_FILE, "w") as f:
        json.dump({"last_run_time": current_time.isoformat()}, f)

def load_processed_ids():
    """
    処理済みのメールIDリストをファイルから読み込み、グローバル変数 ``processed_ids`` を更新する。
    重複処理を防ぐために使用される。
    """
    global processed_ids
    if os.path.exists(PROCESSED_IDS_FILE):
        try:
            with open(PROCESSED_IDS_FILE, "r") as f:
                ids_list = json.load(f)
                processed_ids = set(ids_list)
                logger.info("処理済みメールIDを %d 件ロードしました。", len(processed_ids))
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning("処理済みメールIDファイル '%s' が不正です。新しく作成します。エラー: %s", PROCESSED_IDS_FILE, e)
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
        with open(PROCESSED_IDS_FILE, "w") as f:
            json.dump(list(processed_ids), f)
    except IOError as e:
        logger.error("処理済みメールIDの保存中にエラーが発生しました: %s", e)


def system(mes_body):
    """
    メール本文を正規表現で解析し、主要な家計簿データ（日時、場所、金額）を抽出する。

    抽出後は ``sheet`` 関数を呼び出してスプレッドシートへの記録を行う。

    :param mes_body: 解析対象のメール本文テキスト
    :type mes_body: str
    """
    utilization_datetime_match = re.search(r"(?:利用日|ご利用日時)\s*[:：]\s*(\d{4}/\d{2}/\d{2} \d{2}:\d{2}(?::\d{2})?)", mes_body)
    utilization_datetime = utilization_datetime_match.group(1) if utilization_datetime_match else "N/A"
    utilization_location_match = re.search(r"(?:利用先|ご利用場所)\s*[:：]\s*([^\n]+)", mes_body)
    utilization_location = utilization_location_match.group(1).strip() if utilization_location_match else "N/A"
    utilization_amount_match = re.search(r"ご?利用金額\s*[:：]?\s*([-?\d,]+)円", mes_body)
    utilization_amount_str = utilization_amount_match.group(1) if utilization_amount_match else "N/A"
    utilization_amount = "N/A"
    if utilization_amount_str != "N/A":
        try:
            cleaned_amount_str = utilization_amount_str.replace(',', '')
            utilization_amount = int(cleaned_amount_str)
        except ValueError:
            utilization_amount = utilization_amount_str
            logger.warning("金額 '%s' を数値に変換できませんでした。", utilization_amount_str)
    
    logger.info("抽出結果: 利用日=%s, 利用先=%s, 利用金額=%s", utilization_datetime, utilization_location, utilization_amount)
    
    sheet(utilization_datetime, utilization_location, utilization_amount)


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
    """
    global gc

    if gc is None:
        logger.error("GSpread認証が完了していません。データを書き込めません。")
        return

    try:
        spreadsheet = gc.open_by_key(SPREADSHEET_ID)
        if email_time == "N/A":
            logger.warning("利用日が抽出できませんでした。")
            mailDisco.MailDisco("⚠️ 利用日が抽出できませんでした。").send()
            sheet_name = f'{datetime.datetime.now().month}月'
        elif location == "N/A":
            logger.warning("利用先が抽出できませんでした。")
            mailDisco.MailDisco("⚠️ 利用先が抽出できませんでした。").send()
            sheet_name = "Sheet1"
        elif money == "N/A":
            logger.warning("利用金額が抽出できませんでした。")
            mailDisco.MailDisco("⚠️ 利用金額が抽出できませんでした。").send()
            sheet_name = "Sheet1"
        else:
            sheet_name = timesheet(email_time)
            mailDisco.MailDisco(f"【正常に書き込みました】\n利用先: {location}").send()
        
        try:
            worksheet = spreadsheet.worksheet(sheet_name)
        except gspread.exceptions.WorksheetNotFound:
            logger.error("ワークシート '%s' が見つかりません。", sheet_name)
            try:
                logger.info("フォールバックとして 'Sheet1' への書き込みを試みます。")
                worksheet = spreadsheet.worksheet("Sheet1")
                mailDisco.MailDisco(f"⚠️ ワークシート '{sheet_name}' が見つからず、 'Sheet1' に書き込みました。").send()
            except gspread.exceptions.WorksheetNotFound:
                logger.error("ワークシート 'Sheet1' も見つかりません。書き込みを中止します。")
                return

        
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
            return

        target_row_index = -1
        if not all_values: # シートが完全に空の場合
            worksheet.append_row(data_to_log)
            logger.info("シートが空だったため、1行目にデータ '%s' が追加されました。", data_to_log)
            return
        
        # 3行目(index 2)以降でA列が空の行を探すロジック
        for i, row in enumerate(all_values):
            if i >= 2 and (not row or not row[0]): 
                target_row_index = i + 1
                break
        
        if target_row_index == -1: # A列が空の行で見つからなかった場合
            worksheet.append_row(data_to_log)
            logger.info("A列が空の行が見つからなかったため、最終行にデータ '%s' が追加されました。", data_to_log)
        else:
            range_to_update = f"A{target_row_index}:D{target_row_index}"
            worksheet.update(values=[data_to_log], range_name=range_to_update)
            logger.info("%d行目 (%s) にデータ '%s' が追加されました。", target_row_index, range_to_update, data_to_log)

    except gspread.exceptions.SpreadsheetNotFound:
        logger.error("スプレッドシートが見つかりません。IDが正しいか確認してください: %s", SPREADSHEET_ID)
    except Exception as e:
        logger.error("スプレッドシートへの書き込み中に予期せぬエラーが発生しました: %s", e, exc_info=True)

def timesheet(time):
    """
    利用日時の文字列から、書き込み先のシート名（月）を決定する。

    例: '2023/11/05 12:00' -> '11月'

    :param time: 日時文字列
    :type time: str
    :return: シート名（例: '11月'）
    :rtype: str
    """
    time_object = None
    try:
        # まず HH:MM:SS 形式 (デビットカード用など) で試す
        time_object = datetime.datetime.strptime(time, '%Y/%m/%d %H:%M:%S')
    except ValueError:
        try:
            # 次に HH:MM 形式 (クレジットカード用など) で試す
            time_object = datetime.datetime.strptime(time, '%Y/%m/%d %H:%M')
        except ValueError:
            # どちらも失敗した場合
            logger.warning("timesheet関数: 日付 '%s' の形式が不正です。デフォルトの月を使用します。", time)
            return f'{datetime.datetime.now().month}月'
    
    return f'{time_object.month}月'


def initialize_services():
    """
    Gmail API と Google Sheets API の認証を行い、クライアントを初期化する。

    * Gmail: OAuth2を使用し、トークンがない場合はブラウザ認証を行う。
    * Sheets: サービスアカウントを使用する。

    :return: 両方のサービス初期化に成功した場合 True、失敗した場合 False
    :rtype: bool
    """
    global gc, gmail_service

    logger.info("--- サービス初期化開始 ---")
    
    # Gmail認証
    gmail_creds = None
    if os.path.exists(TOKEN_FILE):
        gmail_creds = GmailCredentials.from_authorized_user_file(TOKEN_FILE, [SCOPES_GMAIL])
    
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
        
        with open(TOKEN_FILE, "w") as token:
            token.write(gmail_creds.to_json())

    try:
        gmail_service = build("gmail", "v1", credentials=gmail_creds)
        logger.info("Gmail APIサービスに接続しました。")
    except HttpError as error:
        logger.error("Gmail APIサービスへの接続中にエラーが発生しました: %s", error, exc_info=True)
        return False # 初期化失敗

    # GSpread認証
    try:
        if not SERVICE_ACCOUNT_FILE:
            logger.error("環境変数 'SERVICE_ACCOUNT_FILE' が設定されていません。 .envファイルを確認してください。")
            return False
        gc = gspread.service_account(filename=SERVICE_ACCOUNT_FILE, scopes=SCOPES_SHEET)
        logger.info("Google スプレッドシートAPIに接続しました。")
    except FileNotFoundError:
        logger.error("サービスアカウントファイル '%s' が見つかりません。 (.envファイルを確認してください)", SERVICE_ACCOUNT_FILE)
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

    logger.info("--- %s: 定時メールチェック実行 ---", datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    
    current_check_time = datetime.datetime.now()
    last_check_time = get_last_run_time()

    # Gmail検索クエリに対象の件名を追加
    query = 'in:inbox subject:"ご利用のお知らせ【三井住友カード】"'
    if last_check_time:
        timestamp = int(last_check_time.timestamp())
        query += f" after:{timestamp}"
        logger.info("最終チェック日時 (%s) 以降を検索。", last_check_time.strftime('%Y-%m-%d %H:%M:%S'))
    else:
        logger.info("初回チェックまたは日時不明。対象件名の全メールを検索候補とします。")

    try:
        results = (
            gmail_service.users()
            .messages()
            .list(userId="me", q=query) 
            .execute()
        )
        messages = results.get("messages", [])

        if not messages:
            logger.info("新しい対象メールは見つかりませんでした。")
        else:
            new_messages_processed_count = 0
            logger.info("%d 件の候補メールが見つかりました。処理を開始します。", len(messages))

            for message_info in messages:
                message_id = message_info["id"]
                
                if message_id in processed_ids:
                    continue
                
                new_messages_processed_count +=1
                logger.info("処理中のメールID: %s", message_id)
                try:
                    msg_detail = (
                        gmail_service.users()
                        .messages()
                        .get(userId="me", id=message_id, format="full")
                        .execute()
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
                        system(body)
                        processed_ids.add(message_id)
                        logger.info("メールID %s の処理が完了し、処理済みとしてマークしました。", message_id)
                    else:
                        logger.warning("メールID %s の本文が見つかりませんでした。スキップします。", message_id)
                        processed_ids.add(message_id)
                
                except HttpError as e_msg_get:
                    logger.error("メールID %s の詳細取得中にエラー: %s", message_id, e_msg_get)
                except Exception as e_proc:
                    logger.error("メールID %s の処理中に予期せぬエラー: %s", message_id, e_proc, exc_info=True)

            if new_messages_processed_count > 0:
                 logger.info("%d 件の新しいメールの処理が完了しました。", new_messages_processed_count)
            else:
                 logger.info("新しい未処理メールはありませんでした（処理済みを除く）。")
        
        save_current_run_time(current_check_time)
        save_processed_ids()

    except HttpError as error:
        logger.error("Gmail API呼び出し中にエラーが発生しました: %s", error, exc_info=True)
        mailDisco.MailDisco("❌ Gmail API呼び出し中にエラーが発生しました").send()
    except Exception as e:
        logger.error("ジョブ実行で予期せぬエラーが発生しました: %s", e, exc_info=True)
        mailDisco.MailDisco("ジョブ実行で予期せぬエラーが発生しました").send()

    logger.info("--- %s: メールチェック完了 ---", datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'))




# --- メインの実行ブロック ---
if __name__ == "__main__":
    
    load_processed_ids() # 最初に処理済みIDをロード

    if initialize_services(): # サービス初期化
        logger.info("サービス初期化完了。スケジューラーを開始します。")
        mailDisco.MailDisco("--- メール監視システムを起動しました ---").send()

        # スケジュールの設定
        schedule.every().hour.at(":00").do(check_mail_job)
        schedule.every().hour.at(":30").do(check_mail_job)


        
        logger.info("スケジューラーを実行中です。")
        logger.info("次回の実行は %s です。", schedule.next_run().strftime('%Y-%m-%d %H:%M:%S'))
        
        # 起動時にすぐ実行したい場合は、以下のコメントを解除
        logger.info("初回実行中...")
        check_mail_job()
        time.sleep(1)

        logger.info("初回実行完了。")

        try:
            while True:
                schedule.run_pending()
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("プログラムを停止しました。")
            mailDisco.MailDisco("--- メール監視システムを停止しました ---").send()

    else:
        logger.error("サービスの初期化に失敗したため、プログラムを終了します。")
        mailDisco.MailDisco("❌ サービスの初期化に失敗しました。プログラムを終了します。").send()
