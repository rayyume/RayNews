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
