"""
既知の不具合（監査で起票済み Issue）について、**現行 main の挙動をそのまま固定**するテスト。

目的:
  - 関係ないリファクタリングでこれらの挙動が「こっそり」変わることを検知する。
  - Issue を修正したときは、該当テストが落ちるのが正しい。落ちたテストを
    「修正後の期待値」に書き換えること（マーカーの Issue 番号で対応箇所が分かる）。

実行例:
  pytest -m known_issue          # 既知不具合の挙動だけを見る
  pytest -m "not known_issue"    # 既知不具合の固定テストを除外する
"""

import base64
import json
from unittest.mock import MagicMock, call, patch

import gspread
import pytest

import src.mailSystem as mailSystem
import src.purpose as purpose_module
from src.purpose import Purpose
from tests.conftest import encode_b64url



def _gc_with(worksheets):
    spreadsheet = MagicMock()

    def _ws(name):
        if name in worksheets:
            return worksheets[name]
        raise gspread.exceptions.WorksheetNotFound(name)

    spreadsheet.worksheet.side_effect = _ws
    gc = MagicMock()
    gc.open_by_key.return_value = spreadsheet
    return gc, spreadsheet


def _gmail_single(body_text, msg_id="m1"):
    gmail = MagicMock()
    gmail.users().messages().list().execute.return_value = {
        "messages": [{"id": msg_id}]
    }
    gmail.users().messages().get().execute.return_value = {
        "payload": {"headers": [], "body": {"data": encode_b64url(body_text)}}
    }
    return gmail


VALID_BODY = (
    "ご利用日時：2026/11/05 12:00\nご利用場所：スーパーA\nご利用金額：1,000円\n"
)


@pytest.mark.known_issue(13)
def test_issue13_sheet_write_failure_still_marks_message_processed(tmp_path):
    ws = MagicMock()
    ws.get_all_values.return_value = []
    ws.append_row.side_effect = RuntimeError("quota exceeded")
    mailSystem.gc, _ = _gc_with({"11月": ws})
    mailSystem.gmail_service = _gmail_single(VALID_BODY)

    with (
        patch("src.mailSystem.mailDisco.MailDisco"),
        patch("src.mailSystem.purpose.Purpose"),
    ):
        mailSystem.check_mail_job()

    ws.append_row.assert_called_once()
    assert "m1" in mailSystem.processed_ids
    assert json.loads(
        (tmp_path / "processed_message_ids.json").read_text(encoding="utf-8")
    ) == ["m1"]


@pytest.mark.known_issue(13)
def test_issue13_marked_processed_even_if_sheet_writes_nothing():
    """sheet() が何も書き込まずに戻っても、system() は成功扱いになり処理済みになる。"""
    mailSystem.gmail_service = _gmail_single(VALID_BODY)
    mailSystem.gc = MagicMock()
    with patch.object(mailSystem, "sheet", return_value=None):
        mailSystem.check_mail_job()
    assert "m1" in mailSystem.processed_ids


@pytest.mark.known_issue(14)
def test_issue14_success_notification_sent_before_write_even_if_write_fails():
    events = []
    ws = MagicMock()
    ws.get_all_values.return_value = []
    ws.append_row.side_effect = lambda row: (
        events.append("write"),
        (_ for _ in ()).throw(RuntimeError("x")),
    )
    mailSystem.gc, _ = _gc_with({"11月": ws})

    disco = MagicMock()
    disco.side_effect = lambda msg: (events.append(("notify", msg)), MagicMock())[1]
    with (
        patch("src.mailSystem.mailDisco.MailDisco", disco),
        patch("src.mailSystem.purpose.Purpose"),
    ):
        mailSystem.sheet("2026/11/05 12:00", "スーパーA", 1000)

    assert events == [
        ("notify", "【正常に書き込みました】\n利用先: スーパーA"),
        "write",
    ]


@pytest.mark.known_issue(14)
def test_issue14_success_notified_even_when_no_worksheet_exists():
    mailSystem.gc, _ = _gc_with({})
    with (
        patch("src.mailSystem.mailDisco.MailDisco") as disco,
        patch("src.mailSystem.purpose.Purpose"),
    ):
        mailSystem.sheet("2026/11/05 12:00", "スーパーA", 1000)
    assert disco.call_args_list == [call("【正常に書き込みました】\n利用先: スーパーA")]


@pytest.mark.known_issue(15)
def test_issue15_rows_with_only_column_a_empty_are_overwritten():
    ws = MagicMock()
    ws.get_all_values.return_value = [["h"], ["h"], ["", "", "", "=SUM(C3:C10)"]]
    mailSystem.gc, _ = _gc_with({"11月": ws})
    with (
        patch("src.mailSystem.mailDisco.MailDisco"),
        patch("src.mailSystem.purpose.Purpose") as p,
    ):
        p.return_value.judge.return_value = "食費"
        mailSystem.sheet("2026/11/05 12:00", "店", 1)
    ws.update.assert_called_once_with(
        values=[["2026/11/05 12:00", "店", 1, "食費"]], range_name="A3:D3"
    )


@pytest.mark.known_issue(16)
def test_issue16_last_run_time_advances_even_when_message_fails(tmp_path):
    from googleapiclient.errors import HttpError

    gmail = MagicMock()
    gmail.users().messages().list().execute.return_value = {"messages": [{"id": "m1"}]}
    gmail.users().messages().get().execute.side_effect = HttpError(
        MagicMock(status=500), b""
    )
    mailSystem.gmail_service = gmail
    mailSystem.gc = MagicMock()

    mailSystem.check_mail_job()

    assert (tmp_path / "last_run_time.json").exists()
    assert "m1" not in mailSystem.processed_ids


@pytest.mark.known_issue(17)
def test_issue17_query_is_inbox_only_and_not_paginated():
    gmail = MagicMock()
    gmail.users().messages().list().execute.return_value = {
        "messages": [{"id": "p1"}],
        "nextPageToken": "NEXT",
    }
    gmail.users().messages().get().execute.return_value = {
        "payload": {"headers": [], "parts": []}
    }
    gmail.users().messages().list.reset_mock()
    mailSystem.gmail_service = gmail
    mailSystem.gc = MagicMock()

    mailSystem.check_mail_job()

    assert gmail.users().messages().list.call_count == 1
    kwargs = gmail.users().messages().list.call_args.kwargs
    assert "pageToken" not in kwargs
    assert kwargs["q"].startswith("in:inbox ")


@pytest.mark.known_issue(18)
def test_issue18_sheet_name_ignores_year():
    assert (
        mailSystem.timesheet("2025/11/05 12:00")
        == mailSystem.timesheet("2026/11/05 12:00")
        == "11月"
    )


@pytest.mark.known_issue(19)
def test_issue19_nested_multipart_body_is_not_found():
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "parts": [
                    {
                        "mimeType": "text/plain",
                        "body": {"data": encode_b64url(VALID_BODY)},
                    }
                ],
            }
        ],
    }
    assert mailSystem.get_email_body(payload) == ""


@pytest.mark.known_issue(19)
def test_issue19_html_only_body_leaks_tags_into_location():
    html = "<p>ご利用日時：2026/11/01 10:00<br>ご利用場所：スーパーA<br>ご利用金額：1,000円</p>"
    with patch.object(mailSystem, "sheet") as mock_sheet:
        mailSystem.system(html)
    mock_sheet.assert_called_once_with(
        "2026/11/01 10:00", "スーパーA<br>ご利用金額：1,000円</p>", 1000
    )


@pytest.mark.known_issue(19)
def test_issue19_empty_plain_part_hides_html_part():
    payload = {
        "parts": [
            {"mimeType": "text/plain", "body": {"data": ""}},
            {"mimeType": "text/html", "body": {"data": encode_b64url("<p>HTML</p>")}},
        ]
    }
    assert mailSystem.get_email_body(payload) == ""


@pytest.mark.known_issue(19)
def test_issue19_empty_location_line_captures_next_line():
    with patch.object(mailSystem, "sheet") as mock_sheet:
        mailSystem.system("ご利用場所：\nご利用金額：500円")
    assert mock_sheet.call_args.args[1] == "ご利用金額：500円"


@pytest.mark.known_issue(20)
def test_issue20_na_rows_are_still_written_and_only_first_missing_field_notified():
    ws = MagicMock()
    ws.get_all_values.return_value = []
    mailSystem.gc, _ = _gc_with({"Sheet1": ws})
    with (
        patch("src.mailSystem.mailDisco.MailDisco") as disco,
        patch("src.mailSystem.purpose.Purpose") as p,
    ):
        p.return_value.judge.return_value = None
        mailSystem.sheet("2026/11/05 12:00", "N/A", "N/A")
    assert disco.call_args_list == [call("⚠️ 利用先が抽出できませんでした。")]
    ws.append_row.assert_called_once_with(["2026/11/05 12:00", "N/A", "N/A", None])


@pytest.mark.known_issue(21)
@pytest.mark.parametrize("raw", ["?100", "--100", "1-2", ",", "?"])
def test_issue21_non_numeric_amount_matches_regex_and_is_passed_as_string(raw):
    with patch.object(mailSystem, "sheet") as mock_sheet:
        mailSystem.system(f"利用金額: {raw}円")
    assert mock_sheet.call_args.args[2] == raw


@pytest.mark.known_issue(22)
def test_issue22_each_write_makes_four_gspread_calls_without_retry():
    ws = MagicMock()
    ws.get_all_values.return_value = [["h"], ["h"], [""]]
    gc, spreadsheet = _gc_with({"11月": ws})
    mailSystem.gc = gc
    with (
        patch("src.mailSystem.mailDisco.MailDisco"),
        patch("src.mailSystem.purpose.Purpose"),
    ):
        mailSystem.sheet("2026/11/05 12:00", "店", 1)
    assert gc.open_by_key.call_count == 1
    assert spreadsheet.worksheet.call_count == 1
    assert ws.get_all_values.call_count == 1
    assert ws.update.call_count == 1


@pytest.mark.known_issue(23)
def test_issue23_state_is_saved_only_once_at_end_of_job(tmp_path):
    saved_during_processing = []

    def _system(_body):
        saved_during_processing.append(
            (tmp_path / "processed_message_ids.json").exists()
        )

    gmail = MagicMock()
    gmail.users().messages().list().execute.return_value = {
        "messages": [{"id": "a"}, {"id": "b"}]
    }
    gmail.users().messages().get().execute.return_value = {
        "payload": {"headers": [], "body": {"data": encode_b64url("x")}}
    }
    mailSystem.gmail_service = gmail
    mailSystem.gc = MagicMock()
    with patch.object(mailSystem, "system", side_effect=_system):
        mailSystem.check_mail_job()
    assert saved_during_processing == [False, False]
    assert (tmp_path / "processed_message_ids.json").exists()


@pytest.mark.known_issue(23)
@pytest.mark.parametrize("content", ["null", "[]", '"2026-11-01T00:00:00"'])
def test_issue23_non_object_last_run_json_raises_attribute_error(tmp_path, content):
    """
    現行仕様: last_run_time.json が JSON として妥当だがオブジェクトでない場合、
    AttributeError が捕捉されずに呼び出し元へ伝播する（check_mail_job の try の外なのでジョブごと落ちる）。
    """
    (tmp_path / "last_run_time.json").write_text(content, encoding="utf-8")
    with pytest.raises(AttributeError):
        mailSystem.get_last_run_time()

    mailSystem.gmail_service = MagicMock()
    mailSystem.gc = MagicMock()
    with pytest.raises(AttributeError):
        mailSystem.check_mail_job()


@pytest.mark.known_issue(23)
@pytest.mark.parametrize(
    "content,expected", [('{"a": 1}', {"a"}), ('"ab"', {"a", "b"})]
)
def test_issue23_processed_ids_accepts_non_list_iterables(tmp_path, content, expected):
    (tmp_path / "processed_message_ids.json").write_text(content, encoding="utf-8")
    mailSystem.load_processed_ids()
    assert mailSystem.processed_ids == expected


@pytest.mark.known_issue(25)
def test_issue25_token_written_with_plain_open(tmp_path):
    (tmp_path / "gmailtoken.json").write_text("{}", encoding="utf-8")
    creds = MagicMock(valid=False, expired=True, refresh_token="r")
    creds.to_json.return_value = '{"t": 1}'
    with (
        patch(
            "src.mailSystem.GmailCredentials.from_authorized_user_file",
            return_value=creds,
        ),
        patch("src.mailSystem.build"),
        patch("src.mailSystem.gspread.service_account"),
    ):
        mailSystem.initialize_services()
    assert (tmp_path / "gmailtoken.json").read_text(encoding="utf-8") == '{"t": 1}'


@pytest.mark.known_issue(26)
def test_issue26_empty_sheet_is_written_to_row1_and_datetime_as_string():
    ws = MagicMock()
    ws.get_all_values.return_value = []
    mailSystem.gc, _ = _gc_with({"11月": ws})
    with (
        patch("src.mailSystem.mailDisco.MailDisco"),
        patch("src.mailSystem.purpose.Purpose") as p,
    ):
        p.return_value.judge.return_value = "食費"
        mailSystem.sheet("2026/11/05 12:00", "店", 1)
    row = ws.append_row.call_args.args[0]
    assert isinstance(row[0], str)
    ws.update.assert_not_called()


@pytest.mark.known_issue(29)
def test_issue29_bom_in_categories_csv_breaks_first_store(monkeypatch, tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "categories.csv").write_bytes(
        "スーパーA,スーパーB\n".encode("utf-8-sig")
    )
    monkeypatch.setattr(
        purpose_module, "__file__", str(tmp_path / "src" / "purpose.py")
    )
    assert Purpose("スーパーA").judge() is None
    assert Purpose("﻿スーパーA").judge() == "食費"
    assert Purpose("スーパーB").judge() == "食費"


@pytest.mark.known_issue(29)
def test_issue29_classification_failure_is_not_notified(caplog):
    ws = MagicMock()
    ws.get_all_values.return_value = []
    mailSystem.gc, _ = _gc_with({"11月": ws})
    with (
        patch("src.mailSystem.mailDisco.MailDisco") as disco,
        patch("src.mailSystem.purpose.Purpose") as p,
    ):
        p.return_value.judge.return_value = None
        mailSystem.sheet("2026/11/05 12:00", "未登録店", 1)
    assert disco.call_args_list == [call("【正常に書き込みました】\n利用先: 未登録店")]


@pytest.mark.known_issue(19)
def test_issue19_iso_2022_jp_body_decodes_via_utf8_path():
    """ISO-2022-JP は 7bit のため UTF-8 デコードが成功してしまい、エスケープシーケンスを含む文字列になる。"""
    raw = "ご利用金額：1,000円".encode("iso-2022-jp")
    decoded = mailSystem.decode_base64_url_safe(base64.urlsafe_b64encode(raw).decode())
    assert decoded == raw.decode("ascii")
    assert "\x1b$B" in decoded
    with patch.object(mailSystem, "sheet") as mock_sheet:
        mailSystem.system(decoded)
    assert mock_sheet.call_args.args == ("N/A", "N/A", "N/A")
