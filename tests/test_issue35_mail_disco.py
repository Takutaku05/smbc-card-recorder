from unittest.mock import MagicMock

import requests

from src import mailDisco


def _make_response(status_code=200, ok=True, json_data=None, headers=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.ok = ok
    resp.headers = headers or {}
    if json_data is not None:
        resp.json.return_value = json_data
    else:
        resp.json.side_effect = ValueError("no json")
    return resp


def test_discord_notifier_sends_successfully():
    session = MagicMock()
    session.post.return_value = _make_response(204, True)

    notifier = mailDisco.DiscordNotifier(webhook_url="https://example.invalid/hook", session=session)
    assert notifier.send("hello") is True
    session.post.assert_called_once_with(
        "https://example.invalid/hook",
        json={"content": "hello", "allowed_mentions": {"parse": []}},
        timeout=mailDisco.DEFAULT_TIMEOUT,
    )


def test_discord_notifier_skips_without_webhook_url(monkeypatch):
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    session = MagicMock()

    notifier = mailDisco.DiscordNotifier(webhook_url=None, session=session)
    assert notifier.send("hello") is False
    session.post.assert_not_called()


def test_discord_notifier_truncates_over_2000_chars():
    session = MagicMock()
    session.post.return_value = _make_response(204, True)

    notifier = mailDisco.DiscordNotifier(webhook_url="https://example.invalid/hook", session=session)
    long_message = "A" * 2500
    assert notifier.send(long_message) is True

    sent_content = session.post.call_args.kwargs["json"]["content"]
    assert len(sent_content) == 2000
    assert sent_content.endswith("...")


def test_discord_notifier_retries_on_429_json_retry_after(monkeypatch):
    sleeps = []
    monkeypatch.setattr(mailDisco.time, "sleep", lambda s: sleeps.append(s))

    session = MagicMock()
    session.post.side_effect = [
        _make_response(429, False, json_data={"retry_after": 1.5}),
        _make_response(204, True),
    ]

    notifier = mailDisco.DiscordNotifier(webhook_url="https://example.invalid/hook", session=session)
    assert notifier.send("rate limited") is True
    assert sleeps == [1.5]
    assert session.post.call_count == 2


def test_discord_notifier_retries_on_429_header_retry_after(monkeypatch):
    sleeps = []
    monkeypatch.setattr(mailDisco.time, "sleep", lambda s: sleeps.append(s))

    session = MagicMock()
    session.post.side_effect = [
        _make_response(429, False, headers={"Retry-After": "2"}),
        _make_response(200, True),
    ]

    notifier = mailDisco.DiscordNotifier(webhook_url="https://example.invalid/hook", session=session)
    assert notifier.send("header retry") is True
    assert sleeps == [2.0]


def test_discord_notifier_returns_false_after_exhausting_429_retries(monkeypatch):
    monkeypatch.setattr(mailDisco.time, "sleep", lambda _s: None)

    session = MagicMock()
    session.post.return_value = _make_response(429, False, json_data={"retry_after": 0.1})

    notifier = mailDisco.DiscordNotifier(
        webhook_url="https://example.invalid/hook",
        session=session,
        max_retries=2,
    )
    assert notifier.send("still limited") is False
    assert session.post.call_count == 3


def test_discord_notifier_handles_request_exception():
    session = MagicMock()
    session.post.side_effect = requests.exceptions.Timeout("timed out")

    notifier = mailDisco.DiscordNotifier(webhook_url="https://example.invalid/hook", session=session)
    assert notifier.send("timeout") is False


def test_mail_disco_backward_compatible_wrapper(monkeypatch):
    session = MagicMock()
    session.post.return_value = _make_response(204, True)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://example.invalid/env-hook")
    monkeypatch.setattr(mailDisco, "_default_session", session)

    assert mailDisco.MailDisco("互換テスト").send() is True
    session.post.assert_called_once()
