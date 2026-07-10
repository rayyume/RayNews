import os
import sqlite3
import unittest
import uuid
from pathlib import Path

import models

ROOT = Path(__file__).resolve().parents[1]


def temp_db_path():
    return ROOT / f"tmp-users-role-test-{uuid.uuid4().hex}.db"


class UsersRoleMigrationTests(unittest.TestCase):
    def test_preview_role_promoted_and_check_constraint_tightened_on_rebuild(self):
        db_path = temp_db_path()
        old_db_file = models.DB_FILE
        old_conn = models._db
        try:
            models.DB_FILE = db_path
            models._db = None

            # Simulate a pre-migration database: old schema still allows 'preview'.
            conn = sqlite3.connect(str(db_path))
            conn.execute(
                """
                CREATE TABLE users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT NOT NULL UNIQUE,
                    password TEXT NOT NULL,
                    nickname TEXT NOT NULL DEFAULT '',
                    role TEXT NOT NULL DEFAULT 'user'
                        CHECK(role IN ('preview', 'user', 'admin')),
                    avatar_url TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )
            conn.execute(
                "INSERT INTO users (id, email, password, nickname, role) VALUES "
                "(1, 'preview@example.com', 'hash', 'previewer', 'preview'), "
                "(2, 'admin@example.com', 'hash', 'admin', 'admin')"
            )
            conn.execute(
                "CREATE TABLE favorites (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, "
                "article_id INTEGER NOT NULL, created_at TEXT NOT NULL DEFAULT (datetime('now')), "
                "UNIQUE(user_id, article_id))"
            )
            conn.execute("INSERT INTO favorites (user_id, article_id) VALUES (1, 42)")
            conn.commit()
            conn.close()

            # Trigger the real migration path (same one that runs on every server start).
            db = models.get_db()

            # Leftover preview row promoted to 'user'.
            role = db.execute("SELECT role FROM users WHERE id = 1").fetchone()[0]
            self.assertEqual(role, "user")

            # All rows (ids, other columns) survived the table rebuild intact.
            rows = db.execute("SELECT id, email, role FROM users ORDER BY id").fetchall()
            self.assertEqual(
                [tuple(r) for r in rows],
                [(1, "preview@example.com", "user"), (2, "admin@example.com", "admin")],
            )

            # Foreign-key-linked child row survived the rebuild.
            fav = db.execute(
                "SELECT user_id, article_id FROM favorites WHERE user_id = 1"
            ).fetchone()
            self.assertEqual(tuple(fav), (1, 42))

            # CHECK constraint is now tightened: 'preview' can no longer be inserted.
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute(
                    "INSERT INTO users (email, password, nickname, role) VALUES "
                    "('new-preview@example.com', 'hash', 'newpreviewer', 'preview')"
                )

            # Migration is idempotent: a fresh get_db() call doesn't error or re-run it.
            db.close()
            models._db = None
            db2 = models.get_db()
            role2 = db2.execute("SELECT role FROM users WHERE id = 1").fetchone()[0]
            self.assertEqual(role2, "user")
        finally:
            models.DB_FILE = old_db_file
            models._db = old_conn
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(str(db_path) + suffix)
                except FileNotFoundError:
                    pass


if __name__ == "__main__":
    unittest.main()
