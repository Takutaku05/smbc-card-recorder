import base64
import sys
import types

import pytest

try:
    import gspread  # noqa: F401
except ImportError:
    class _WorksheetNotFound(Exception):
        pass

    class _SpreadsheetNotFound(Exception):
        pass

    sys.modules["gspread"] = types.SimpleNamespace(
        exceptions=types.SimpleNamespace(
            WorksheetNotFound=_WorksheetNotFound,
            SpreadsheetNotFound=_SpreadsheetNotFound,
        )
    )

try:
    import schedule  # noqa: F401
except ImportError:
    sys.modules["schedule"] = types.SimpleNamespace()

from src import mailSystem


def _b64(text: str, encoding: str = "utf-8") -> str:
    return base64.urlsafe_b64encode(text.encode(encoding)).decode("ascii")


def test_flat_text_plain_utf8():
    sample = "ご利用日時：2026/01/05 12:34\nご利用先：テストストア\nご利用金額：1,234円"
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/plain", "body": {"data": _b64(sample)}},
            {"mimeType": "text/html", "body": {"data": _b64("<p>HTML</p>")}},
        ],
    }
    assert mailSystem.get_email_body(payload) == sample


def test_direct_leaf_body_without_parts():
    sample = "ご利用金額：500円"
    payload = {"body": {"data": _b64(sample)}}
    assert mailSystem.get_email_body(payload) == sample


def test_nested_multipart_mixed_extracts_plain_text():
    sample = "ご利用日時：2026/02/10 09:00\nご利用先：コンビニ\nご利用金額：350円"
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "parts": [
                    {"mimeType": "text/html", "body": {"data": _b64("<p>HTML版</p>")}},
                    {"mimeType": "text/plain", "body": {"data": _b64(sample)}},
                ],
            },
            {
                "mimeType": "application/pdf",
                "filename": "receipt.pdf",
                "body": {"data": _b64("PDFDATA")},
            },
        ],
    }
    assert mailSystem.get_email_body(payload) == sample


def test_nested_html_only_strips_tags_and_unescapes():
    html_src = (
        "<html><head><style>body{color:#000;}</style>"
        "<script>alert(1);</script></head>"
        "<body><table>"
        "<tr><td>ご利用日時：</td><td>2026/03/01 18:20</td></tr>"
        "<tr><td>ご利用先：</td><td>A&amp;B ショップ</td></tr>"
        "<tr><td>ご利用金額：</td><td>2,500円</td></tr>"
        "</table><br/>ご利用ありがとうございました</body></html>"
    )
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "parts": [
                    {"mimeType": "text/html", "body": {"data": _b64(html_src)}},
                ],
            }
        ],
    }
    extracted = mailSystem.get_email_body(payload)
    assert "alert(1)" not in extracted
    assert "color:#000" not in extracted
    assert "<" not in extracted
    assert "ご利用日時： 2026/03/01 18:20" in extracted
    assert "ご利用先： A&B ショップ" in extracted
    assert "ご利用金額： 2,500円" in extracted


@pytest.mark.parametrize("with_header", [True, False])
def test_iso_2022_jp_decodes_cleanly(with_header):
    sample = "ご利用金額：1,000円"
    part = {"mimeType": "text/plain", "body": {"data": _b64(sample, "iso-2022-jp")}}
    if with_header:
        part["headers"] = [{"name": "Content-Type", "value": 'text/plain; charset="ISO-2022-JP"'}]
    assert mailSystem.get_email_body(part) == sample


@pytest.mark.parametrize("encoding", ["cp932", "euc_jp"])
def test_cp932_and_euc_jp_fallback(encoding):
    sample = "ご利用先：株式会社テスト　渋谷店\nご利用金額：4,200円"
    payload = {"mimeType": "text/plain", "body": {"data": _b64(sample, encoding)}}
    assert mailSystem.get_email_body(payload) == sample


def test_unknown_charset_header_falls_back_to_utf8():
    sample = "ご利用金額：980円"
    payload = {
        "mimeType": "text/plain",
        "headers": [{"name": "Content-Type", "value": "text/plain; charset=x-unknown-codec"}],
        "body": {"data": _b64(sample, "utf-8")},
    }
    assert mailSystem.get_email_body(payload) == sample


def test_undecodable_bytes_raise_unicode_decode_error():
    bad_bytes = b"\x81\x20\xff\x80"
    b64 = base64.urlsafe_b64encode(bad_bytes).decode("ascii")
    with pytest.raises(UnicodeDecodeError):
        mailSystem.decode_base64_url_safe(b64)


def test_attachment_only_or_empty_payload_returns_empty_string():
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {
                "mimeType": "text/plain",
                "filename": "note.txt",
                "body": {"data": _b64("添付ファイル")},
            }
        ],
    }
    assert mailSystem.get_email_body(payload) == ""
    assert mailSystem.get_email_body({}) == ""
