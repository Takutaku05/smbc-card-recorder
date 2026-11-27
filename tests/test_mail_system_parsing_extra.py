"""
system() / parse_mail_body() / get_email_body() / decode_base64_url_safe() の追加回帰テスト。
"""

import base64
import copy
from unittest.mock import patch

import pytest

import src.mailSystem as mailSystem
from tests.conftest import encode_b64url

CREDIT_MAIL = """\
三井住友カード 太郎 様

いつも三井住友カードをご利用いただきありがとうございます。
お客様のカードご利用内容をお知らせいたします。

ご利用カード：三井住友カード（NL）
◇利用日：2026/11/27 18:45
◇利用先：セブン－イレブン
◇利用取引：買物
◇利用金額：1,280円

ご利用日時：2026/11/27 18:45
ご利用場所：セブン－イレブン
ご利用金額：1,280円

※本メールは送信専用です。
"""

DEBIT_MAIL = """\
デビットカードのご利用がありました。

利用日：2026/03/09 07:58:12
利用先：ＪＲ東日本モバイルＳｕｉｃａ
利用金額：3,000円
"""


def _call(body):
    with patch.object(mailSystem, "sheet", return_value=True) as mock_sheet:
        mailSystem.system(body)
    mock_sheet.assert_called_once()
    return mock_sheet.call_args.args


class TestSystemRealisticBodies:
    def test_credit_card_mail(self):
        assert _call(CREDIT_MAIL) == ("2026/11/27 18:45", "セブン－イレブン", 1280)

    def test_debit_card_mail(self):
        assert _call(DEBIT_MAIL) == (
            "2026/03/09 07:58:12",
            "ＪＲ東日本モバイルＳｕｉｃａ",
            3000,
        )

    def test_crlf_line_endings(self):
        body = "ご利用日時：2026/11/01 10:00\r\nご利用場所：スーパーA\r\nご利用金額：100円\r\n"
        assert _call(body) == ("2026/11/01 10:00", "スーパーA", 100)

    def test_first_occurrence_wins_when_fields_repeat(self):
        body = (
            "ご利用日時：2026/01/01 10:00\n"
            "ご利用日時：2026/02/02 11:00\n"
            "ご利用場所：A店\n"
            "ご利用場所：B店\n"
            "ご利用金額：100円\n"
            "ご利用金額：200円\n"
        )
        assert _call(body) == ("2026/01/01 10:00", "A店", 100)

    def test_fields_in_any_order(self):
        body = "ご利用金額：500円\nご利用場所：店X\nご利用日時：2026/06/01 09:00"
        assert _call(body) == ("2026/06/01 09:00", "店X", 500)

    def test_spaces_around_colon_are_allowed(self):
        body = "ご利用日時 ： 2026/11/01 10:00\nご利用場所 ： 店\nご利用金額 ： 1円"
        assert _call(body) == ("2026/11/01 10:00", "店", 1)

    def test_location_with_tabs_and_inner_spaces_is_stripped_only_at_edges(self):
        body = "ご利用場所：\t ABC  マート 新宿店 \t\nご利用金額：5円"
        assert _call(body) == ("N/A", "ABC  マート 新宿店", 5)

    def test_location_keeps_symbols(self):
        body = "利用先：ＡＭＡＺＯＮ．ＣＯ．ＪＰ（ｱﾏｿﾞﾝ）/ 決済#123\n利用金額：99円"
        assert _call(body)[1] == "ＡＭＡＺＯＮ．ＣＯ．ＪＰ（ｱﾏｿﾞﾝ）/ 決済#123"

    @pytest.mark.parametrize("label", ["利用日", "ご利用日時"])
    @pytest.mark.parametrize("colon", [":", "："])
    def test_datetime_label_and_colon_variants(self, label, colon):
        assert _call(f"{label}{colon}2026/08/15 12:00")[0] == "2026/08/15 12:00"

    @pytest.mark.parametrize("label", ["利用先", "ご利用場所"])
    @pytest.mark.parametrize("colon", [":", "："])
    def test_location_label_and_colon_variants(self, label, colon):
        assert _call(f"{label}{colon}店")[1] == "店"

    @pytest.mark.parametrize("label", ["利用金額", "ご利用金額"])
    @pytest.mark.parametrize("colon", [":", "：", ""])
    def test_amount_label_and_colon_variants(self, label, colon):
        assert _call(f"{label}{colon}1,000円")[2] == 1000

    def test_datetime_flexible_variants_are_normalized(self):
        assert _call("ご利用日時：2026/1/1 10:00")[0] == "2026/1/1 10:00"
        assert _call("ご利用日時：2026-11-01 10:00")[0] == "2026/11/01 10:00"

    @pytest.mark.parametrize(
        "body",
        [
            "ご利用日時：2026/11/01",
            "ご利用日時：26/11/01 10:00",
            "ご利用日時：2026/11/01 10時00分",
        ],
    )
    def test_datetime_unsupported_formats_become_na(self, body):
        assert _call(body)[0] == "N/A"

    def test_datetime_seconds_are_optional_and_kept_when_present(self):
        assert _call("利用日：2026/11/01 10:00:59")[0] == "2026/11/01 10:00:59"
        assert _call("利用日：2026/11/01 10:00")[0] == "2026/11/01 10:00"

    def test_amount_allows_space_before_yen_and_unicode_minus(self):
        assert _call("ご利用金額：1,000 円")[2] == 1000
        assert _call("ご利用金額：▲1,500円")[2] == -1500

    @pytest.mark.parametrize(
        "body",
        [
            "ご利用金額：１，０００円",
            "ご利用金額：￥1,000円",
            "ご利用金額：1,000",
            "ご利用金額：1,000JPY",
        ],
    )
    def test_amount_unsupported_formats_become_na(self, body):
        assert _call(body)[2] == "N/A"

    def test_amount_type_is_int_when_parsed(self):
        amount = _call("利用金額：12,345円")[2]
        assert amount == 12345
        assert type(amount) is int

    def test_label_must_be_followed_by_value_on_regex_terms(self):
        assert _call("利用：2026/11/01 10:00\n場所：店\n金額：100円") == (
            "N/A",
            "N/A",
            "N/A",
        )

    def test_empty_body(self):
        assert _call("") == ("N/A", "N/A", "N/A")

    def test_sheet_receives_positional_args_in_order(self):
        with patch.object(mailSystem, "sheet", return_value=True) as mock_sheet:
            mailSystem.system(DEBIT_MAIL)
        args, kwargs = mock_sheet.call_args
        assert kwargs == {}
        assert len(args) == 3


class TestGetEmailBodyExtra:
    def test_returns_first_plain_part_when_multiple_plain_parts(self):
        payload = {
            "parts": [
                {"mimeType": "text/plain", "body": {"data": encode_b64url("1つ目")}},
                {"mimeType": "text/plain", "body": {"data": encode_b64url("2つ目")}},
            ]
        }
        assert mailSystem.get_email_body(payload) == "1つ目"

    def test_first_html_part_wins_when_multiple_html_parts_and_no_plain(self):
        payload = {
            "parts": [
                {"mimeType": "text/html", "body": {"data": encode_b64url("<p>1</p>")}},
                {"mimeType": "text/html", "body": {"data": encode_b64url("<p>2</p>")}},
            ]
        }
        assert mailSystem.get_email_body(payload) == "1\n"

    def test_parts_take_precedence_over_top_level_body(self):
        payload = {
            "body": {"data": encode_b64url("トップレベル本文")},
            "parts": [
                {"mimeType": "text/html", "body": {"data": encode_b64url("パート本文")}}
            ],
        }
        assert mailSystem.get_email_body(payload) == "パート本文"

    def test_top_level_body_used_when_parts_key_exists_but_empty(self):
        payload = {"parts": [], "body": {"data": encode_b64url("トップ本文")}}
        assert mailSystem.get_email_body(payload) == "トップ本文"

    def test_parts_without_mime_type_decode_as_plain_text(self):
        payload = {"parts": [{"body": {"data": encode_b64url("x")}}]}
        assert mailSystem.get_email_body(payload) == "x"

    def test_attachment_parts_are_skipped_before_plain(self):
        payload = {
            "parts": [
                {"mimeType": "application/pdf", "body": {"attachmentId": "abc"}},
                {"mimeType": "image/png", "body": {"data": encode_b64url("png")}},
                {"mimeType": "text/plain", "body": {"data": encode_b64url("本文")}},
            ]
        }
        assert mailSystem.get_email_body(payload) == "本文"

    def test_single_part_html_body_is_converted_to_text(self):
        payload = {
            "mimeType": "text/html",
            "body": {"data": encode_b64url("<b>HTML</b>")},
        }
        assert mailSystem.get_email_body(payload) == "HTML"

    def test_does_not_mutate_payload(self):
        payload = {
            "parts": [
                {"mimeType": "text/plain", "body": {"data": encode_b64url("本文")}}
            ],
        }
        snapshot = copy.deepcopy(payload)
        mailSystem.get_email_body(payload)
        assert payload == snapshot

    def test_return_type_is_str(self):
        assert isinstance(mailSystem.get_email_body({}), str)
        assert isinstance(
            mailSystem.get_email_body({"body": {"data": encode_b64url("a")}}), str
        )


class TestDecodeExtra:
    def test_uses_urlsafe_alphabet_not_standard(self):
        raw = b"\x81\x00\xff\xfe"
        safe = base64.urlsafe_b64encode(raw).decode()
        with pytest.raises(UnicodeDecodeError):
            mailSystem.decode_base64_url_safe(safe)

    def test_iso_2022_jp_fallback_is_used_when_utf8_fails(self):
        calls = []

        class FakeBytes(bytes):
            def decode(self, encoding="utf-8", errors="strict"):
                calls.append(encoding)
                if encoding == "utf-8":
                    raise UnicodeDecodeError("utf-8", b"", 0, 1, "fake")
                return "ISO経由"

        with patch.object(
            mailSystem.base64, "urlsafe_b64decode", return_value=FakeBytes(b"x")
        ):
            assert mailSystem.decode_base64_url_safe("eA") == "ISO経由"
        assert calls == ["utf-8", "iso-2022-jp"]

    @pytest.mark.parametrize("pad", ["", "=", "=="])
    def test_existing_padding_is_tolerated(self, pad):
        encoded = base64.urlsafe_b64encode("ab".encode()).decode().rstrip("=") + pad
        assert mailSystem.decode_base64_url_safe(encoded) == "ab"

    def test_padding_is_added_to_reach_multiple_of_four(self):
        with patch.object(
            mailSystem.base64, "urlsafe_b64decode", return_value=b""
        ) as spy:
            mailSystem.decode_base64_url_safe("abcde")
        spy.assert_called_once_with("abcde===")

        with patch.object(
            mailSystem.base64, "urlsafe_b64decode", return_value=b""
        ) as spy:
            mailSystem.decode_base64_url_safe("abcd")
        spy.assert_called_once_with("abcd")
