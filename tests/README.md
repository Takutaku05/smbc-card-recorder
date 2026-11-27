# テスト

全モジュールおよび各 Issue 修正を検証する回帰テストスイートです。外部サービス（Gmail / Google Sheets / Discord）は全てモックし、
状態ファイルは `tmp_path`（`__main__` テストは一時サンドボックス）に隔離され、ログは実 `logs/` に書き込まず、`.env` の `DISCORD_WEBHOOK_URL` も無効化されるため、本番の `config/`・`logs/`・認証情報には触れません（`conftest.py`）。

```bash
.venv/bin/pip install -r requirements-dev.txt     # pytest / pytest-cov / ruff の導入
.venv/bin/python -m pytest                       # 全テスト
.venv/bin/python -m pytest --cov=src --cov-branch  # カバレッジ（行・分岐とも100%）
.venv/bin/python -m pytest -m known_issue        # 未対応の既知課題の現行挙動だけ
.venv/bin/python -m pytest -m "not known_issue"  # 既知課題の固定テストを除外
```

PR と `main` / `develop` への push では `.github/workflows/tests.yml` が Python 3.11 / 3.12 で同じテストを実行します（カバレッジ100%未満で失敗）。

## ファイル構成

| ファイル | 対象 |
| --- | --- |
| `test_mail_system_decoding.py` / `test_mail_system_parsing*.py` | Base64デコード、多段multipart/HTML/文字コード本文抽出、正規表現による抽出 |
| `test_mail_system_sheet*.py` | スプレッドシート書き込み、A〜D列空き行探索、キャッシュ、Discord通知、N/Aスキップ |
| `test_mail_system_state.py` | `last_run_time.json`（UTC aware）/ `processed_message_ids.json`（パージ・移行・アトミック保存） |
| `test_mail_system_services_and_job*.py` | 認証初期化、定期ジョブ（ページング・昇順処理・リトライ・状態保存・ログ） |
| `test_main_entrypoint_extra.py` | `__main__` の起動シーケンス、スケジュール登録、メインループ、SIGTERM |
| `test_purpose.py` / `test_mail_disco.py` / `test_logger_config.py` / `test_support_modules_extra.py` | 補助モジュール（NFKC/部分一致/BOM/CP932、429リトライ/2000文字制限、0600ログ権限） |
| `test_config_contracts.py` | 定数・最小権限スコープ・環境変数名・設定サンプル・`.gitignore` の契約 |
| `test_issue13_*.py` 〜 `test_issue35_*.py` | 各 Issue（#13〜#17, #19〜#25, #27, #29, #30, #32〜#35）の個別回帰テスト |
| `test_pr58_review_fixes.py` | PR #58 レビュー指摘への対応（Sheets 再試行上限、送信済み・下書き除外と検索範囲の上限、失敗メールの再試行打ち切り、SIGTERM の安全な停止、整形済み HTML、権限変更失敗時の継続） |
| `test_known_issues.py` | 監査 Issue の統合検証および未対応課題（#18, #26 等）の現行挙動固定 |

## `known_issue` マーカーについて

`@pytest.mark.known_issue(N)` が付いたテストは、未対応の Issue #N の挙動を「現在こう動いている」として固定しています。
Issue を修正すると該当テストが落ちるのが正しい挙動なので、修正後の期待値に書き換えてマーカーを外してください
（関係ない変更でこれらが落ちた場合は、意図しない挙動変更です）。
