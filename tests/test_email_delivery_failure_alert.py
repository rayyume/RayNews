import web_server


def _recorded_reason(notices):
    body = notices[0][3]
    return body.removeprefix("邮件推送服务不可用。原因：").split("\n\n", 1)[0]


def test_first_delivery_failure_alerts_each_admin_once(monkeypatch):
    stored = {"email_delivery_failure_alerted": "0"}
    notices = []
    monkeypatch.setattr(web_server, "claim_app_state_flag", lambda key: (
        False if stored.get(key) == "1" else (stored.__setitem__(key, "1") or True)
    ))
    monkeypatch.setattr(web_server, "set_app_state", lambda key, value: stored.__setitem__(key, str(value)))
    monkeypatch.setattr(web_server, "list_users", lambda: [
        {"id": 1, "role": "admin"}, {"id": 2, "role": "user"}, {"id": 3, "role": "admin"},
    ])
    monkeypatch.setattr(web_server, "add_notification", lambda *args: notices.append(args) or 1)

    web_server._note_email_delivery_failure("Resend error: invalid_api_key")
    web_server._note_email_delivery_failure("Resend error: invalid_api_key")

    assert [notice[0] for notice in notices] == [1, 3]
    assert all(notice[1] == "email_delivery_failed" for notice in notices)
    assert all(notice[2] == "邮件推送服务不可用" for notice in notices)


def test_delivery_failure_reason_is_redacted_normalized_and_limited(monkeypatch):
    notices = []
    monkeypatch.setattr(web_server, "claim_app_state_flag", lambda key: True)
    monkeypatch.setattr(web_server, "list_users", lambda: [{"id": 1, "role": "admin"}])
    monkeypatch.setattr(web_server, "add_notification", lambda *args: notices.append(args) or 1)

    web_server._note_email_delivery_failure("RESEND_API_KEY=super-secret\n" + "x" * 400)

    safe_reason = _recorded_reason(notices)
    assert "RESEND_API_KEY" not in safe_reason
    assert "super-secret" not in safe_reason
    assert "\n" not in safe_reason
    assert len(safe_reason) <= 300


def test_delivery_failure_reason_redacts_configured_key_value_when_bare(monkeypatch):
    notices = []
    monkeypatch.setenv("RESEND_API_KEY", "configured-super-secret")
    monkeypatch.setattr(web_server, "claim_app_state_flag", lambda key: True)
    monkeypatch.setattr(web_server, "list_users", lambda: [{"id": 1, "role": "admin"}])
    monkeypatch.setattr(web_server, "add_notification", lambda *args: notices.append(args) or 1)

    web_server._note_email_delivery_failure(
        "Resend rejected credential configured-super-secret"
    )

    assert "configured-super-secret" not in _recorded_reason(notices)


def test_delivery_failure_reason_redacts_quoted_labeled_key_value(monkeypatch):
    notices = []
    monkeypatch.setenv("RESEND_API_KEY", "configured secret with spaces")
    monkeypatch.setattr(web_server, "claim_app_state_flag", lambda key: True)
    monkeypatch.setattr(web_server, "list_users", lambda: [{"id": 1, "role": "admin"}])
    monkeypatch.setattr(web_server, "add_notification", lambda *args: notices.append(args) or 1)

    web_server._note_email_delivery_failure(
        "config={'RESEND_API_KEY': 'configured secret with spaces'}"
    )

    safe_reason = _recorded_reason(notices)
    assert "RESEND_API_KEY" not in safe_reason
    assert "configured secret with spaces" not in safe_reason


def test_admin_insert_failure_does_not_skip_later_admins(monkeypatch):
    attempted = []
    notices = []
    monkeypatch.setattr(web_server, "claim_app_state_flag", lambda key: True)
    monkeypatch.setattr(web_server, "list_users", lambda: [
        {"id": 1, "role": "admin"},
        {"id": 2, "role": "admin"},
        {"id": 3, "role": "admin"},
    ])

    def add_notification(user_id, *args):
        attempted.append(user_id)
        if user_id == 1:
            raise RuntimeError("temporary insert failure")
        notices.append((user_id, *args))
        return 1

    monkeypatch.setattr(web_server, "add_notification", add_notification)

    web_server._note_email_delivery_failure("resend unavailable")

    assert attempted == [1, 2, 3]
    assert [notice[0] for notice in notices] == [2, 3]


def test_no_delivery_releases_suppression_so_a_later_failure_retries(monkeypatch):
    stored = {"email_delivery_failure_alerted": "0"}
    attempts = []
    notices = []

    def claim(key):
        if stored.get(key) == "1":
            return False
        stored[key] = "1"
        return True

    def set_state(key, value):
        stored[key] = str(value)

    def add_notification(*args):
        attempts.append(args[0])
        if len(attempts) == 1:
            raise RuntimeError("temporary insert failure")
        notices.append(args)
        return 1

    monkeypatch.setattr(web_server, "claim_app_state_flag", claim)
    monkeypatch.setattr(web_server, "set_app_state", set_state)
    monkeypatch.setattr(web_server, "list_users", lambda: [{"id": 1, "role": "admin"}])
    monkeypatch.setattr(web_server, "add_notification", add_notification)

    web_server._note_email_delivery_failure("resend unavailable")
    assert stored["email_delivery_failure_alerted"] == "0"

    web_server._note_email_delivery_failure("resend unavailable")

    assert attempts == [1, 1]
    assert [notice[0] for notice in notices] == [1]
    assert stored["email_delivery_failure_alerted"] == "1"


def test_delivery_failure_alert_never_uses_email_capable_notification_helpers(monkeypatch):
    notices = []
    monkeypatch.setattr(web_server, "claim_app_state_flag", lambda key: True)
    monkeypatch.setattr(web_server, "list_users", lambda: [{"id": 1, "role": "admin"}])
    monkeypatch.setattr(web_server, "add_notification", lambda *args: notices.append(args) or 1)

    def forbidden(*args, **kwargs):
        raise AssertionError("email-capable notification helper called")

    monkeypatch.setattr(web_server, "_notify_user", forbidden)
    monkeypatch.setattr(web_server, "_notify_admins", forbidden)
    monkeypatch.setattr(web_server, "_send_notification_email", forbidden)

    web_server._note_email_delivery_failure("resend unavailable")

    assert [notice[0] for notice in notices] == [1]


def test_missing_resend_key_from_notification_email_alerts_admins(monkeypatch):
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    reasons = []
    monkeypatch.setattr(web_server, "_note_email_delivery_failure", reasons.append)

    assert web_server._send_notification_email(7, "标题", "正文") is False

    assert reasons == ["RESEND_API_KEY 未配置"]


def test_notification_email_default_format_keeps_body_literal(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "test-key")
    monkeypatch.setattr(
        web_server,
        "_notification_recipient",
        lambda user_id: "user@example.com",
    )
    monkeypatch.setattr(web_server, "_clear_email_delivery_failure_alert", lambda: None)
    sent = []
    monkeypatch.setattr(
        web_server,
        "send_email",
        lambda *args, **kwargs: sent.append((args, kwargs)) or {"id": "sent"},
    )

    assert web_server._send_notification_email(
        7,
        "标题",
        "**literal**\n<b>x</b>",
    ) is True

    html_body = sent[0][0][3]
    assert "**literal**<br>&lt;b&gt;x&lt;/b&gt;" in html_body
    assert "<strong>literal</strong>" not in html_body


def test_notification_email_markdown_format_uses_safe_renderer(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "test-key")
    monkeypatch.setenv("RAYNEWS_PUBLIC_URL", "https://news.example")
    monkeypatch.setattr(
        web_server,
        "_notification_recipient",
        lambda user_id: "user@example.com",
    )
    monkeypatch.setattr(web_server, "_clear_email_delivery_failure_alert", lambda: None)
    sent = []
    monkeypatch.setattr(
        web_server,
        "send_email",
        lambda *args, **kwargs: sent.append((args, kwargs)) or {"id": "sent"},
    )

    assert web_server._send_notification_email(
        7,
        "标题",
        "#### 小节\n\n![图](https://img.example/a.png)",
        fmt="markdown",
    ) is True

    html_body = sent[0][0][3]
    assert "<h4>小节</h4>" in html_body
    assert (
        "https://news.example/img-cache?url="
        "https%3A%2F%2Fimg.example%2Fa.png"
    ) in html_body


def test_success_clears_suppression_and_later_failure_alerts_again(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "test-key")
    monkeypatch.setattr(web_server, "_notification_recipient", lambda user_id: "user@example.com")
    cleared = []
    failures = []
    monkeypatch.setattr(web_server, "_clear_email_delivery_failure_alert", lambda: cleared.append(True))
    monkeypatch.setattr(web_server, "_note_email_delivery_failure", failures.append)
    monkeypatch.setattr(web_server, "send_email", lambda *args, **kwargs: {"id": "sent"})

    assert web_server._send_notification_email(7, "标题", "正文") is True
    monkeypatch.setattr(web_server, "send_email", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("invalid api key")))

    assert web_server._send_notification_email(7, "标题", "正文") is False
    assert cleared == [True]
    assert failures == ["invalid api key"]
