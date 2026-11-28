"""
監査で起票された Issue（#13〜#29）に対する回帰テスト。

- develop で修正済みの Issue（#13〜#17, #19〜#25, #29 BOM等）は修正後の期待値を検証する。
- 未対応の既知課題（#18, #26, #29 未分類通知なし）は `@pytest.mark.known_issue(N)` を付与し、
  現行挙動を固定している。

実行例:
  pytest -m known_issue          # 未解消の既知課題のみ実行
  pytest -m "not known_issue"    # 未解消の既知課題を除外して実行
"""

import base64
import json
import stat
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


def test_issue13_sheet_write_failure_does_not_mark_message_processed(tmp_path):
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
    assert "m1" not in mailSystem.processed_ids
    assert json.loads(
        (tmp_path / "processed_message_ids.json").read_text(encoding="utf-8")
    ) == {}


def test_issue13_not_marked_processed_when_sheet_returns_false():
    """sheet() が False（書き込み失敗）を返した場合は処理済みに登録されない。"""
    mailSystem.gmail_service = _gmail_single(VALID_BODY)
    mailSystem.gc = MagicMock()
    with (
        patch("src.mailSystem.mailDisco.MailDisco"),
        patch.object(mailSystem, "sheet", return_value=False),
    ):
        mailSystem.check_mail_job()
    assert "m1" not in mailSystem.processed_ids


def test_issue14_failure_notification_sent_after_write_attempt_fails():
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
        assert mailSystem.sheet("2026/11/05 12:00", "スーパーA", 1000) is False

    assert events == [
        "write",
        (
            "notify",
            "❌ スプレッドシートへの書き込みに失敗しました。\n利用先: スーパーA / 金額: 1000\nエラー: x",
        ),
    ]


def test_issue14_error_notified_when_no_worksheet_exists():
    mailSystem.gc, _ = _gc_with({})
    with (
        patch("src.mailSystem.mailDisco.MailDisco") as disco,
        patch("src.mailSystem.purpose.Purpose"),
    ):
        assert mailSystem.sheet("2026/11/05 12:00", "スーパーA", 1000) is False
    assert disco.call_args_list == [
        call(
            "❌ ワークシート '11月' も 'Sheet1' も見つからず、利用先: スーパーA / 金額: 1000 を記録できませんでした。"
        )
    ]


def test_issue15_rows_with_formula_in_column_d_are_not_overwritten():
    ws = MagicMock()
    ws.get_all_values.return_value = [["h"], ["h"], ["", "", "", "=SUM(C3:C10)"]]
    mailSystem.gc, _ = _gc_with({"11月": ws})
    with (
        patch("src.mailSystem.mailDisco.MailDisco"),
        patch("src.mailSystem.purpose.Purpose") as p,
    ):
        p.return_value.judge.return_value = "食費"
        assert mailSystem.sheet("2026/11/05 12:00", "店", 1) is True
    ws.update.assert_not_called()
    ws.append_row.assert_called_once_with(["2026/11/05 12:00", "店", 1, "食費"])


def test_issue16_last_run_time_does_not_advance_when_message_fails(tmp_path):
    from googleapiclient.errors import HttpError

    gmail = MagicMock()
    gmail.users().messages().list().execute.return_value = {"messages": [{"id": "m1"}]}
    gmail.users().messages().get().execute.side_effect = HttpError(
        MagicMock(status=500), b""
    )
    mailSystem.gmail_service = gmail
    mailSystem.gc = MagicMock()

    with patch("src.mailSystem.mailDisco.MailDisco"):
        mailSystem.check_mail_job()

    assert not (tmp_path / "last_run_time.json").exists()
    assert "m1" not in mailSystem.processed_ids


def test_issue17_query_excludes_inbox_filter_and_paginates_all_pages():
    gmail = MagicMock()
    gmail.users().messages().list.reset_mock()
    gmail.users().messages().list().execute.side_effect = [
        {"messages": [{"id": "p1"}], "nextPageToken": "NEXT"},
        {"messages": [{"id": "p2"}]},
    ]
    gmail.users().messages().get().execute.return_value = {
        "payload": {"headers": [], "body": {"data": encode_b64url(VALID_BODY)}}
    }
    gmail.users().messages().list.reset_mock()
    mailSystem.gmail_service = gmail
    mailSystem.gc = MagicMock()

    with patch.object(mailSystem, "system", return_value=True):
        mailSystem.check_mail_job()

    assert gmail.users().messages().list.call_count == 2
    first_kwargs = gmail.users().messages().list.call_args_list[0].kwargs
    second_kwargs = gmail.users().messages().list.call_args_list[1].kwargs
    assert "in:inbox" not in first_kwargs["q"]
    assert "-in:trash -in:spam" in first_kwargs["q"]
    assert "pageToken" not in first_kwargs
    assert second_kwargs["pageToken"] == "NEXT"


@pytest.mark.known_issue(18)
def test_issue18_sheet_name_ignores_year():
    assert (
        mailSystem.timesheet("2025/11/05 12:00")
        == mailSystem.timesheet("2026/11/05 12:00")
        == "11月"
    )


def test_issue19_nested_multipart_body_is_extracted():
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
    assert mailSystem.get_email_body(payload) == VALID_BODY


def test_issue19_html_only_body_strips_tags_before_parsing():
    html_text = "<p>ご利用日時：2026/11/01 10:00<br>ご利用場所：スーパーA<br>ご利用金額：1,000円</p>"
    payload = {
        "mimeType": "text/html",
        "body": {"data": encode_b64url(html_text)},
    }
    body = mailSystem.get_email_body(payload)
    with patch.object(mailSystem, "sheet", return_value=True) as mock_sheet:
        mailSystem.system(body)
    mock_sheet.assert_called_once_with("2026/11/01 10:00", "スーパーA", 1000)


def test_issue19_empty_plain_part_falls_back_to_html_part():
    payload = {
        "parts": [
            {"mimeType": "text/plain", "body": {"data": ""}},
            {"mimeType": "text/html", "body": {"data": encode_b64url("<p>HTML</p>")}},
        ]
    }
    assert mailSystem.get_email_body(payload) == "HTML\n"


def test_issue19_empty_location_line_does_not_capture_next_line():
    with patch.object(mailSystem, "sheet", return_value=mailSystem.SHEET_SKIPPED) as mock_sheet:
        mailSystem.system("ご利用場所：\nご利用金額：500円")
    assert mock_sheet.call_args.args == ("N/A", "N/A", 500)


def test_issue20_na_rows_are_skipped_and_all_missing_fields_notified():
    ws = MagicMock()
    mailSystem.gc, _ = _gc_with({"Sheet1": ws})
    with (
        patch("src.mailSystem.mailDisco.MailDisco") as disco,
        patch("src.mailSystem.purpose.Purpose") as p,
    ):
        result = mailSystem.sheet("2026/11/05 12:00", "N/A", "N/A")
    assert result == mailSystem.SHEET_SKIPPED
    assert disco.call_args_list == [
        call(
            "⚠️ 以下の項目を抽出できなかったため記録を中止しました: 利用先・利用金額\n"
            "利用日: 2026/11/05 12:00 / 利用先: N/A / 金額: N/A"
        )
    ]
    p.assert_not_called()
    ws.append_row.assert_not_called()
    ws.update.assert_not_called()


@pytest.mark.parametrize("raw", ["?100", "--100", "1-2", ",", "?"])
def test_issue21_non_numeric_amount_becomes_na(raw):
    with patch.object(mailSystem, "sheet", return_value=mailSystem.SHEET_SKIPPED) as mock_sheet:
        mailSystem.system(f"利用金額: {raw}円")
    assert mock_sheet.call_args.args[2] == "N/A"


def test_issue22_consecutive_writes_reuse_cached_spreadsheet_and_worksheet():
    ws = MagicMock()
    ws.get_all_values.return_value = [["h"], ["h"], [""]]
    gc, spreadsheet = _gc_with({"11月": ws})
    mailSystem.gc = gc
    with (
        patch("src.mailSystem.mailDisco.MailDisco"),
        patch("src.mailSystem.purpose.Purpose"),
    ):
        mailSystem.sheet("2026/11/05 12:00", "店1", 1)
        mailSystem.sheet("2026/11/06 12:00", "店2", 2)
    assert gc.open_by_key.call_count == 1
    assert spreadsheet.worksheet.call_count == 1
    assert ws.get_all_values.call_count == 2
    assert ws.update.call_count == 2


def test_issue23_state_is_saved_immediately_per_message(tmp_path):
    saved_during_processing = []

    def _system(_body):
        saved_during_processing.append(
            (tmp_path / "processed_message_ids.json").exists()
        )
        return True

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
    # 1件目処理前はまだ無く、2件目処理時点では1件目の保存が完了している
    assert saved_during_processing == [False, True]
    assert set(
        json.loads((tmp_path / "processed_message_ids.json").read_text(encoding="utf-8"))
    ) == {"a", "b"}


@pytest.mark.parametrize("content", ["null", "[]", '"2026-11-01T00:00:00"', "123"])
def test_issue23_non_object_last_run_json_recovers_gracefully(tmp_path, content):
    """last_run_time.json が JSON として妥当でも辞書でない場合は警告・通知のうえリセットする。"""
    (tmp_path / "last_run_time.json").write_text(content, encoding="utf-8")
    with patch("src.mailSystem.mailDisco.MailDisco") as disco:
        assert mailSystem.get_last_run_time() is None
    disco.assert_called_once()


def test_issue23_processed_ids_accepts_dict_and_rejects_raw_string(tmp_path):
    (tmp_path / "processed_message_ids.json").write_text(
        '{"a": "2026-11-27T00:00:00+00:00"}', encoding="utf-8"
    )
    mailSystem.load_processed_ids()
    assert mailSystem.processed_ids == {"a"}

    (tmp_path / "processed_message_ids.json").write_text('"ab"', encoding="utf-8")
    with patch("src.mailSystem.mailDisco.MailDisco") as disco:
        mailSystem.load_processed_ids()
    assert mailSystem.processed_ids == set()
    disco.assert_called_once()


def test_issue25_token_written_atomically_with_0600_mode(tmp_path):
    token_file = tmp_path / "gmailtoken.json"
    token_file.write_text("{}", encoding="utf-8")
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
        assert mailSystem.initialize_services() is True
    assert token_file.read_text(encoding="utf-8") == '{"t": 1}'
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600


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


def test_issue29_bom_in_categories_csv_loads_first_store(monkeypatch, tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "categories.csv").write_bytes(
        "スーパーA,スーパーB\n".encode("utf-8-sig")
    )
    monkeypatch.setattr(
        purpose_module, "__file__", str(tmp_path / "src" / "purpose.py")
    )
    assert Purpose("スーパーA").judge() == "食費"
    assert Purpose("スーパーB").judge() == "食費"


@pytest.mark.known_issue(29)
def test_issue29_classification_failure_is_not_notified():
    ws = MagicMock()
    ws.get_all_values.return_value = []
    mailSystem.gc, _ = _gc_with({"11月": ws})
    with (
        patch("src.mailSystem.mailDisco.MailDisco") as disco,
        patch("src.mailSystem.purpose.Purpose") as p,
    ):
        p.return_value.judge.return_value = None
        mailSystem.sheet("2026/11/05 12:00", "未登録店", 1)
    assert disco.call_args_list == [
        call("【正常に書き込みました】\nシート: 11月 (1行目)\n利用先: 未登録店\n金額: 1")
    ]


def test_issue19_iso_2022_jp_body_decodes_via_esc_detection():
    """ISO-2022-JP は ESC シーケンス検知により UTF-8 より先に正しくデコードされる。"""
    raw = "ご利用金額：1,000円".encode("iso-2022-jp")
    decoded = mailSystem.decode_base64_url_safe(base64.urlsafe_b64encode(raw).decode())
    assert decoded == "ご利用金額：1,000円"
    with patch.object(mailSystem, "sheet", return_value=mailSystem.SHEET_SKIPPED) as mock_sheet:
        mailSystem.system(decoded)
    assert mock_sheet.call_args.args == ("N/A", "N/A", 1000)
