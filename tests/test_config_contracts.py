"""
設定ファイル・モジュール定数・外部インターフェースの「契約」を固定するテスト。

コードの振る舞いではなく、運用者が依存している値（ファイルパス、環境変数名、
Gmail検索クエリ、OAuthスコープ、ロガー名など）が意図せず変わらないことを保証する。
"""

import csv
import json
import re
from pathlib import Path

import pytest

import src.logger_config as logger_config
import src.mailSystem as mailSystem
import src.purpose as purpose_module
from src.purpose import Purpose

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_FILES = [
    "src/mailSystem.py",
    "src/purpose.py",
    "src/mailDisco.py",
    "src/logger_config.py",
]


def _getenv_names(source):
    """ソース中の os.getenv(...) が読む環境変数名を、クォート種別・空白を問わず抽出する。"""
    return set(re.findall(r"""os\.getenv\(\s*["']([A-Z_]+)["']""", source))


class TestModuleConstants:
    def test_gmail_scope_is_readonly(self):
        # 読み取り専用スコープであること（誤って modify/send 権限を要求しない）
        assert (
            mailSystem.SCOPES_GMAIL == "https://www.googleapis.com/auth/gmail.readonly"
        )

    def test_sheet_scopes(self):
        assert mailSystem.SCOPES_SHEET == [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]

    def test_base_dir_is_repository_root(self):
        assert Path(mailSystem.BASE_DIR).resolve() == REPO_ROOT

    def test_state_file_paths_are_under_config_dir(self):
        """conftest が差し替える前の本来のパスを、ソースから再計算して検証する。"""
        source = (REPO_ROOT / "src/mailSystem.py").read_text(encoding="utf-8")
        for const, filename in [
            ("TOKEN_FILE", "gmailtoken.json"),
            ("LAST_RUN_FILE", "last_run_time.json"),
            ("PROCESSED_IDS_FILE", "processed_message_ids.json"),
        ]:
            # フォーマッタによる改行・空白・クォート種別の変更には影響されないようにする
            pattern = (
                rf"{const}\s*=\s*os\.path\.join\(\s*BASE_DIR\s*,\s*"
                rf"[\"']config[\"']\s*,\s*[\"']{re.escape(filename)}[\"']\s*,?\s*\)"
            )
            assert re.search(pattern, source), const

    def test_env_driven_settings_read_expected_variable_names(self):
        source = (REPO_ROOT / "src/mailSystem.py").read_text(encoding="utf-8")
        assert _getenv_names(source) == {"SERVICE_ACCOUNT_FILE", "SPREADSHEET_ID"}
        disco_source = (REPO_ROOT / "src/mailDisco.py").read_text(encoding="utf-8")
        assert _getenv_names(disco_source) == {"DISCORD_WEBHOOK_URL"}

    def test_global_state_initial_types(self):
        # conftest によるリセット後の型（本番起動時と同じ初期状態）
        assert mailSystem.gc is None
        assert mailSystem.gmail_service is None
        assert mailSystem.processed_ids == set()
        assert isinstance(mailSystem.processed_ids, set)

    def test_root_logger_name(self):
        assert logger_config._ROOT_LOGGER_NAME == "smbc_card_recorder"

    @pytest.mark.parametrize(
        "module,expected",
        [
            ("src.mailSystem", "smbc_card_recorder.src.mailSystem"),
            ("src.purpose", "smbc_card_recorder.src.purpose"),
            ("src.mailDisco", "smbc_card_recorder.src.mailDisco"),
        ],
    )
    def test_module_logger_names(self, module, expected):
        import importlib

        mod = importlib.import_module(module)
        assert mod.logger.name == expected


class TestEnvExample:
    def _parse_env(self, path):
        keys = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, _, value = line.partition("=")
            keys[key.strip()] = value.strip().strip('"')
        return keys

    def test_env_example_declares_every_variable_the_code_reads(self):
        env_keys = set(self._parse_env(REPO_ROOT / ".env.example"))

        used = set()
        for rel in SRC_FILES:
            used |= _getenv_names((REPO_ROOT / rel).read_text(encoding="utf-8"))

        assert used == {"SERVICE_ACCOUNT_FILE", "SPREADSHEET_ID", "DISCORD_WEBHOOK_URL"}
        assert used <= env_keys

    def test_env_example_service_account_points_into_config_dir(self):
        env = self._parse_env(REPO_ROOT / ".env.example")
        assert env["SERVICE_ACCOUNT_FILE"].startswith("config/")


class TestConfigExamples:
    @pytest.mark.parametrize("name", ["credentials.json.example", "sheet.json.example"])
    def test_json_examples_are_valid_json(self, name):
        json.loads((REPO_ROOT / "config" / name).read_text(encoding="utf-8"))

    def test_credentials_example_is_installed_app_client(self):
        """InstalledAppFlow.from_client_secrets_file が受け付ける 'installed' 形式であること。"""
        data = json.loads(
            (REPO_ROOT / "config" / "credentials.json.example").read_text(
                encoding="utf-8"
            )
        )
        assert "installed" in data
        for key in (
            "client_id",
            "client_secret",
            "auth_uri",
            "token_uri",
            "redirect_uris",
        ):
            assert key in data["installed"]

    def test_sheet_example_is_service_account(self):
        data = json.loads(
            (REPO_ROOT / "config" / "sheet.json.example").read_text(encoding="utf-8")
        )
        assert data["type"] == "service_account"
        for key in ("client_email", "private_key", "token_uri"):
            assert key in data

    def test_categories_example_has_one_row_per_category(self):
        with open(
            REPO_ROOT / "config" / "categories.csv.example", encoding="utf-8"
        ) as f:
            rows = list(csv.reader(f))
        assert len(rows) == len(Purpose.CATEGORY_ORDER)
        assert all(any(cell.strip() for cell in row) for row in rows)

    def test_categories_example_loads_through_purpose(self, monkeypatch, tmp_path):
        """サンプルCSVを categories.csv として配置したとき、Purpose が全行を正しく分類できること。"""
        (tmp_path / "src").mkdir()
        (tmp_path / "config").mkdir()
        (tmp_path / "config" / "categories.csv").write_bytes(
            (REPO_ROOT / "config" / "categories.csv.example").read_bytes()
        )
        monkeypatch.setattr(
            purpose_module, "__file__", str(tmp_path / "src" / "purpose.py")
        )

        with open(
            REPO_ROOT / "config" / "categories.csv.example", encoding="utf-8"
        ) as f:
            rows = list(csv.reader(f))
        for category, row in zip(Purpose.CATEGORY_ORDER, rows):
            for store in row:
                if store.strip():
                    assert Purpose(store.strip()).judge() == category


class TestGitignoreProtectsSecrets:
    @pytest.mark.parametrize(
        "path",
        [
            ".env",
            "config/gmailtoken.json",
            "config/credentials.json",
            "config/sheet.json",
            "config/categories.csv",
            "config/last_run_time.json",
            "config/processed_message_ids.json",
            "logs/app.log",
        ],
    )
    def test_secret_and_state_files_are_ignored(self, path):
        import shutil
        import subprocess

        if shutil.which("git") is None or not (REPO_ROOT / ".git").exists():
            pytest.skip("git リポジトリ外では検証しない")
        result = subprocess.run(
            ["git", "check-ignore", "-q", "--no-index", path],
            cwd=REPO_ROOT,
            check=False,
        )
        assert result.returncode == 0, f"{path} が .gitignore されていません"
