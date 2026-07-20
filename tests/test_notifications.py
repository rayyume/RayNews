import os
import unittest
import uuid
from pathlib import Path

import models

ROOT = Path(__file__).resolve().parents[1]


def temp_db_path():
    return ROOT / f"tmp-notifications-test-{uuid.uuid4().hex}.db"


class NotificationsModelTests(unittest.TestCase):
    def setUp(self):
        self.db_path = temp_db_path()
        self.old_db_file = models.DB_FILE
        self.old_conn = models._db
        models.DB_FILE = self.db_path
        models._db = None
        self.db = models.get_db()
        self.user_a = models.create_user("a@example.com", "pw", "A")["id"]
        self.user_b = models.create_user("b@example.com", "pw", "B")["id"]

    def tearDown(self):
        self.db.close()
        models.DB_FILE = self.old_db_file
        models._db = self.old_conn
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(str(self.db_path) + suffix)
            except FileNotFoundError:
                pass

    def test_add_list_and_unread_count(self):
        self.assertEqual(models.count_unread_notifications(self.user_a), 0)
        nid1 = models.add_notification(self.user_a, "share_revoked", "标题1", "正文1")
        nid2 = models.add_notification(self.user_a, "share_revoked", "标题2", "正文2")
        self.assertTrue(nid2 > nid1)

        items = models.list_notifications(self.user_a)
        # Newest first.
        self.assertEqual([i["id"] for i in items], [nid2, nid1])
        self.assertEqual(items[0]["title"], "标题2")
        self.assertIsNone(items[0]["read_at"])
        self.assertEqual(models.count_unread_notifications(self.user_a), 2)

    def test_mark_read_is_scoped_and_idempotent(self):
        nid = models.add_notification(self.user_a, "share_revoked", "标题", "正文")

        # Wrong user cannot mark it read.
        self.assertFalse(models.mark_notification_read(self.user_b, nid))
        self.assertEqual(models.count_unread_notifications(self.user_a), 1)

        # Owner can.
        self.assertTrue(models.mark_notification_read(self.user_a, nid))
        self.assertEqual(models.count_unread_notifications(self.user_a), 0)
        items = models.list_notifications(self.user_a)
        self.assertIsNotNone(items[0]["read_at"])

        # Marking an already-read row again is a no-op, not an error.
        self.assertFalse(models.mark_notification_read(self.user_a, nid))

    def test_users_only_see_their_own_notifications(self):
        models.add_notification(self.user_a, "share_revoked", "给A的", "")
        models.add_notification(self.user_b, "share_revoked", "给B的", "")

        a_items = models.list_notifications(self.user_a)
        b_items = models.list_notifications(self.user_b)
        self.assertEqual([i["title"] for i in a_items], ["给A的"])
        self.assertEqual([i["title"] for i in b_items], ["给B的"])

    def test_old_unread_notification_is_not_starved_by_a_full_page_of_newer_read_ones(self):
        # Regression test: list_notifications() must never let an unread row
        # fall off the LIMIT window just because enough newer *read* rows
        # exist, or its unread badge would be permanently stuck (nothing left
        # in the list to click to clear it).
        old_unread_id = models.add_notification(self.user_a, "share_revoked", "旧的未读", "")
        for i in range(5):
            nid = models.add_notification(self.user_a, "share_revoked", f"新的已读{i}", "")
            models.mark_notification_read(self.user_a, nid)

        items = models.list_notifications(self.user_a, limit=3)
        self.assertIn(old_unread_id, [i["id"] for i in items])
        self.assertEqual(models.count_unread_notifications(self.user_a), 1)


if __name__ == "__main__":
    unittest.main()
