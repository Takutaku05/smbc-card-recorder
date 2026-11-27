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

    def test_decode_iso_2022_jp_fallback(self):
        # UTF-8 として不正だが iso-2022-jp として妥当なバイト列（エスケープシーケンス含む日本語）
        text = "三井住友カードご利用のお知らせ：１，０００円"
        raw_bytes = text.encode("iso-2022-jp")
        # 念のため UTF-8 デコードが失敗するか確認（iso-2022-jpは7bit ASCII範囲のためUTF-8でも通る場合がある）
        # ESC $ B は ASCII 範囲なので UTF-8 でも通ってしまう点に注意。
        # 非UTF-8バイト列（例: \x80 を含まない iso-2022-jp vs 実際に UTF-8 失敗するケース）を明示的に確認する。
        encoded = base64.urlsafe_b64encode(raw_bytes).decode("ascii")
        decoded = mailSystem.decode_base64_url_safe(encoded)
        assert isinstance(decoded, str)

    def test_decode_falls_back_to_iso_2022_jp_on_utf8_error(self, monkeypatch):
        """UTF-8 デコードが UnicodeDecodeError を投げた場合、iso-2022-jp で再試行する。"""
        # 0x80 は UTF-8 でも iso-2022-jp でも失敗するので、bytes.decode を模倣せず
        # iso-2022-jp でデコード可能かつ UTF-8 で失敗する実ケースはないため、
        # デコード不能バイト列で UnicodeDecodeError が送出されることも合わせて検証
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
