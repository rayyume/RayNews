"""邮件推送 may not be saved without an address to push to.

_deliver_daily_summary_email() only collects subscribers that have a to_email,
so persisting daily_summary_enabled=1 with an empty address left the user with a
toggle that looked on and delivered nothing.
"""

import json
import os
import sys
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import models
import web_server


def _put(payload, user_id, role="user"):
    from flask import g
    with web_server.app.test_request_context("/settings", method="PUT", json=payload):
        g.user_id = user_id
        g.user_role = role
        result = web_server.update_settings.__wrapped__()
    if isinstance(result, tuple):
        return result[0].get_json(), result[1]
    return result.get_json(), 200


class EmailPushRequiresAnAddressTests(unittest.TestCase):
    def setUp(self):
        self.db_path = ROOT / f"tmp-notify-required-{uuid.uuid4().hex}.db"
        self.old_db_file = models.DB_FILE
        models.close_db()
        models.DB_FILE = self.db_path
        models.get_db()
        self.user_id = models.create_user("u@example.com", "pw", "U")["id"]

    def tearDown(self):
        models.close_db()
        models.DB_FILE = self.old_db_file
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(str(self.db_path) + suffix)
            except FileNotFoundError:
                pass

    def _settings(self):
        return models.get_user_settings(self.user_id) or {}

    def test_enabling_email_push_without_an_address_is_refused(self):
        body, status = _put({
            "daily_summary_enabled": 1,
            "notification_config": {"resend": {"to_email": ""}},
        }, self.user_id)

        self.assertEqual(status, 400)
        self.assertIn("接收邮箱", body["error"])
        self.assertFalse(self._settings().get("daily_summary_enabled"))

    def test_a_blank_address_does_not_count_as_filled_in(self):
        _, status = _put({
            "daily_summary_enabled": 1,
            "notification_config": {"resend": {"to_email": "   "}},
        }, self.user_id)
        self.assertEqual(status, 400)

    def test_enabling_email_push_with_an_address_saves(self):
        body, status = _put({
            "daily_summary_enabled": 1,
            "notification_config": {"resend": {"to_email": "me@example.com"}},
        }, self.user_id)

        self.assertEqual(status, 200)
        self.assertNotIn("error", body)
        settings = self._settings()
        self.assertEqual(settings["daily_summary_enabled"], 1)
        self.assertEqual(
            json.loads(settings["notification_config"])["resend"]["to_email"],
            "me@example.com",
        )

    def test_clearing_the_address_while_email_push_stays_on_is_refused(self):
        _put({
            "daily_summary_enabled": 1,
            "notification_config": {"resend": {"to_email": "me@example.com"}},
        }, self.user_id)

        # The toggle isn't in this payload at all — the stored value decides.
        _, status = _put({"notification_config": {"resend": {"to_email": ""}}}, self.user_id)

        self.assertEqual(status, 400)
        settings = self._settings()
        self.assertEqual(
            json.loads(settings["notification_config"])["resend"]["to_email"],
            "me@example.com",
        )

    def test_turning_email_push_off_with_no_address_is_allowed(self):
        body, status = _put({
            "daily_summary_enabled": 0,
            "notification_config": {"resend": {"to_email": ""}},
        }, self.user_id)

        self.assertEqual(status, 200)
        self.assertNotIn("error", body)

    def test_the_in_app_toggle_alone_never_needs_an_address(self):
        body, status = _put({"daily_summary_inapp_enabled": 1}, self.user_id)

        self.assertEqual(status, 200)
        self.assertNotIn("error", body)
        self.assertEqual(self._settings()["daily_summary_inapp_enabled"], 1)

    def test_an_unrelated_save_is_not_blocked_by_a_missing_address(self):
        # Email push is off and no address is set: saving the theme must still work.
        body, status = _put({"theme_preference": "dark"}, self.user_id)

        self.assertEqual(status, 200)
        self.assertEqual(self._settings()["theme_preference"], "dark")


class ResendAddressParsingTests(unittest.TestCase):
    def test_reads_both_the_payload_dict_and_the_stored_json_string(self):
        self.assertEqual(
            web_server._resend_to_email({"resend": {"to_email": " a@b.com "}}), "a@b.com")
        self.assertEqual(
            web_server._resend_to_email('{"resend": {"to_email": "a@b.com"}}'), "a@b.com")

    def test_missing_malformed_and_empty_shapes_read_as_no_address(self):
        for value in (None, "", "{}", "not json", {}, {"resend": None},
                      {"resend": "a@b.com"}, {"resend": {}}, {"resend": {"to_email": None}}):
            self.assertEqual(web_server._resend_to_email(value), "", repr(value))


class NotifyTabFrontendContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")

    def test_the_save_button_refuses_before_it_reaches_the_network(self):
        start = self.html.index("async function saveNotifyConfig()")
        block = self.html[start:self.html.index("async function testNotification()", start)]
        self.assertIn("开启邮件推送前请先填写接收邮箱", block)
        # The guard must run before the PUT, and hand focus to the empty field.
        self.assertLess(block.index("开启邮件推送前请先填写接收邮箱"), block.index("fetch('/settings'"))
        self.assertIn("notifyToEmail').focus()", block)
        # Whitespace must not pass as an address, here or in what gets stored.
        self.assertIn("document.getElementById('notifyToEmail').value.trim()", block)
        self.assertIn("to_email: toEmail,", block)


if __name__ == "__main__":
    unittest.main()
