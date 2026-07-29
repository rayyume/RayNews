import web_server


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

    body = notices[0][3]
    safe_reason = body.removeprefix("邮件推送服务不可用。原因：").split("\n\n", 1)[0]
    assert "RESEND_API_KEY" not in safe_reason
    assert "super-secret" not in safe_reason
    assert "\n" not in safe_reason
    assert len(safe_reason) <= 300


def test_missing_resend_key_from_notification_email_alerts_admins(monkeypatch):
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    reasons = []
    monkeypatch.setattr(web_server, "_note_email_delivery_failure", reasons.append)

    assert web_server._send_notification_email(7, "标题", "正文") is False

    assert reasons == ["RESEND_API_KEY 未配置"]


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
