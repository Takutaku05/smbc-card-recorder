import base64

import pytest

import src.mailSystem as mailSystem
from tests.conftest import encode_b64url


class TestDecodeBase64UrlSafe:
    def test_decode_utf8_ascii_with_padding(self):
        encoded = encode_b64url("Hello, SMBC Card!", encoding="utf-8", strip_padding=False)
        assert mailSystem.decode_base64_url_safe(encoded) == "Hello, SMBC Card!"

    @pytest.mark.parametrize("text", ["a", "ab", "abc", "abcd", "テスト文字列"])
    def test_decode_utf8_without_padding_all_mod_lengths(self, text):
        encoded = encode_b64url(text, encoding="utf-8", strip_padding=True)
        assert "=" not in encoded
        assert mailSystem.decode_base64_url_safe(encoded) == text

    def test_decode_utf8_japanese_and_emoji(self):
        text = "ご利用日時：2026/05/01 12:34\nご利用場所：テスト店舗 🛒\nご利用金額：1,500円"
        encoded = encode_b64url(text, encoding="utf-8", strip_padding=True)
        assert mailSystem.decode_base64_url_safe(encoded) == text

    def test_decode_url_safe_characters_dash_and_underscore(self):
        # URL-safe Base64 の '-' と '_' を含むバイト列
        raw = ">>??日本語テスト??<<".encode("utf-8")
        encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        assert mailSystem.decode_base64_url_safe(encoded) == ">>??日本語テスト??<<"

    # ISO-2022-JP 本文は 7bit のため UTF-8 デコードがそのまま成功してしまう現行挙動は
    # test_known_issues.py::test_issue19_iso_2022_jp_body_decodes_via_utf8_path で、
    # UTF-8 失敗時に iso-2022-jp で再試行する経路は test_mail_system_parsing_extra.py で検証している。

    def test_decode_bytes_invalid_in_both_encodings_raises_unicode_decode_error(self):
        """UTF-8 でも iso-2022-jp でもデコードできないバイト列は UnicodeDecodeError を送出する。"""
        invalid_bytes = b"\xff\xfe\xfd"
        encoded = base64.urlsafe_b64encode(invalid_bytes).decode("ascii")
        with pytest.raises(UnicodeDecodeError):
            mailSystem.decode_base64_url_safe(encoded)

    def test_decode_empty_string(self):
        assert mailSystem.decode_base64_url_safe("") == ""


class TestGetEmailBody:
    def test_extracts_text_plain_from_multipart(self):
        payload = {
            "parts": [
                {
                    "mimeType": "text/plain",
                    "body": {"data": encode_b64url("プレーンテキスト本文", strip_padding=True)},
                },
                {
                    "mimeType": "text/html",
                    "body": {"data": encode_b64url("<p>HTML本文</p>", strip_padding=True)},
                },
            ]
        }
        assert mailSystem.get_email_body(payload) == "プレーンテキスト本文"

    def test_prefers_text_plain_even_when_html_comes_first(self):
        payload = {
            "parts": [
                {
                    "mimeType": "text/html",
                    "body": {"data": encode_b64url("<p>HTML本文</p>", strip_padding=True)},
                },
                {
                    "mimeType": "text/plain",
                    "body": {"data": encode_b64url("後ろのプレーンテキスト", strip_padding=True)},
                },
            ]
        }
        assert mailSystem.get_email_body(payload) == "後ろのプレーンテキスト"

    def test_falls_back_to_text_html_when_no_plain_part(self):
        payload = {
            "parts": [
                {
                    "mimeType": "text/html",
                    "body": {"data": encode_b64url("<div>HTML本文のみ</div>", strip_padding=True)},
                },
            ]
        }
        assert mailSystem.get_email_body(payload) == "<div>HTML本文のみ</div>"

    def test_skips_plain_part_without_data_and_uses_html(self):
        payload = {
            "parts": [
                {"mimeType": "text/plain", "body": {}},
                {
                    "mimeType": "text/html",
                    "body": {"data": encode_b64url("<p>フォールバックHTML</p>", strip_padding=True)},
                },
            ]
        }
        assert mailSystem.get_email_body(payload) == "<p>フォールバックHTML</p>"

    def test_extracts_single_part_body_directly(self):
        payload = {
            "mimeType": "text/plain",
            "body": {"data": encode_b64url("単一パート本文", strip_padding=True)},
        }
        assert mailSystem.get_email_body(payload) == "単一パート本文"

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"parts": []},
            {"body": {}},
            {"parts": [{"mimeType": "application/pdf", "body": {"data": encode_b64url("pdf")}}]},
            {"parts": [{"mimeType": "text/plain"}]},
            {"parts": [{"mimeType": "text/html", "body": {}}]},
        ],
    )
    def test_returns_empty_string_when_body_unavailable(self, payload):
        assert mailSystem.get_email_body(payload) == ""
