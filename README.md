# smbc-card-recorder
三井住友カードの利用通知メールをGmailから取得し、Googleスプレッドシートに自動記録するPythonスクリプト。
## 使い方
### 仮想環境の作り方
convertGmailForsheetディレクトリに移動
#### 仮想環境の作成
python3 -m venv <<仮想環境名:例venv>>
`python3 -m venv venv`
##### Windowsの場合
.\venv\Scripts\Activate.ps1
#### 仮想環境の立ち上げ
source <<仮想環境名>>/bin/activate
`source venv/bin/activate`
#### ライブラリのインストール
`pip install python-dotenv requests google-api-python-client google-auth-httplib2 google-auth-oauthlib gspread`
`pip install schedule`
`pip install python-dotenv`
`pip install Flask`
`pip install google-generativeai --break-system-packages`
`sudo apt update`
`sudo apt install -y php php-sqlite3`
#### サーバーの起動
`php -S 0.0.0.0:8080`

## 必要なファイル
### envファイル
SERVICE_ACCOUNT_FILE,SPREADSHEET_ID,DISCORD_WEBHOOK_URL,GEMINI_API_KEYが必要
#### SERVICE_ACCOUNT_FILEの取得方法

#### SPREADSHEET_IDの取得方法
GoogleスプレッドシートのIDは、そのシートの**URL**から簡単に取得できます。

1.  ブラウザで、IDを知りたいGoogleスプレッドシートを開きます。
2.  ブラウザのアドレスバーに表示されているURLを見ます。

URLは通常、以下のような形式になっています。

`https://docs.google.com/spreadsheets/d/`**`[ここにある長い英数字の文字列]`**`/edit#gid=0`

この、`/d/` と `/edit` の間にある長い英数字の文字列が **スプレッドシートID** です。

##### 例

##### もしURLが
`https://docs.google.com/spreadsheets/d/`**`1aBcD_eFgHiJkLmNoPqRsTuVwXyZ_12345abcdefg`**`/edit`

##### スプレッドシートIDは
**`1aBcD_eFgHiJkLmNoPqRsTuVwXyZ_12345abcdefg`**

になります。
#### DISCORD_WEBHOOK_URLの取得方法

#### GEMINI_API_KEYの取得方法
googleAIstudioから取得
APIキーを作成をクリック
任意のキーの名前を入力後、プロジェクトを作成、任意のプロジェクト名を入力し、キーを作成

#### envファイルの書き方
```dotenv
SERVICE_ACCOUNT_FILE="service_account.json"
SPREADSHEET_ID='XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX'
DISCORD_WEBHOOK_URL='https://discord.com/api/webhooks/your/webhook_url'
GEMINI_API_KEY="AIXXXXXXXXXXXXXXXXXXXXXXXXXXXX"
```
### credentials.json
Gmail APIの初回ユーザー認証情報が保存されているファイル
#### 取得方法

### your-service-account-file.json
Googleスプレッドシートへのプログラム認証が保存されているファイル
#### 取得方法

## 作成されるファイル
token.json,last_run.json,processed_ids.jsonが作成されます

## スプレットシートの変更方法
### 前提
同じアカウントでのスプレットシートであること
必要なファイルがディレクトリーにあること
### 変更方法
1.新しく書き込みたいスプレッドシートを開きます。
2.envファイルのSPREADSHEET_IDを変更(SPREADSHEET_IDの取得方法を参照)
3.右上の [共有] ボタンをクリックします。
4.SERVICE_ACCOUNT_FILEに記載されている client_email のメールアドレス（例: my-service-account@my-project.iam.gserviceaccount.com のような形式）を「ユーザーやグループを追加」の欄に入力します。
5.権限を「編集者」にして、[送信]（または[共有]）をクリックします。
