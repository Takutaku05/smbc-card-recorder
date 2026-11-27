# テスト

main の現行挙動を固定する回帰テストです。外部サービス（Gmail / Google Sheets / Discord）は全てモックし、
状態ファイルは `tmp_path` に隔離されるため、本番の `config/` や認証情報には触れません（`conftest.py`）。

```bash
.venv/bin/python -m pytest                       # 全テスト
.venv/bin/python -m pytest --cov=src --cov-branch  # カバレッジ（行・分岐とも100%）
.venv/bin/python -m pytest -m known_issue        # 既知不具合の現行挙動だけ
.venv/bin/python -m pytest -m "not known_issue"  # 既知不具合の固定テストを除外
```

## ファイル構成

| ファイル | 対象 |
| --- | --- |
| `test_mail_system_decoding.py` / `test_mail_system_parsing*.py` | Base64デコード、本文抽出、正規表現による抽出 |
| `test_mail_system_sheet*.py` | スプレッドシート書き込み、空き行探索、Discord通知、シート名決定 |
| `test_mail_system_state.py` | `last_run_time.json` / `processed_message_ids.json` の読み書き |
| `test_mail_system_services_and_job*.py` | 認証初期化、定期ジョブ（API引数・リトライ・状態保存・ログ） |
| `test_main_entrypoint_extra.py` | `__main__` の起動シーケンス、スケジュール登録、メインループ |
| `test_purpose.py` / `test_mail_disco.py` / `test_logger_config.py` / `test_support_modules_extra.py` | 補助モジュール |
| `test_config_contracts.py` | 定数・環境変数名・設定サンプル・`.gitignore` の契約 |
| `test_known_issues.py` | 既知不具合（Issue #13〜）の**現行**挙動 |

## `known_issue` マーカーについて

`@pytest.mark.known_issue(N)` が付いたテストは、Issue #N の不具合を「現在こう動いている」として固定しています。
Issue を修正すると該当テストが落ちるのが正しい挙動なので、修正後の期待値に書き換えてください
（関係ない変更でこれらが落ちた場合は、意図しない挙動変更です）。
