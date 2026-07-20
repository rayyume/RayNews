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
    def test_fresh_install_gets_tightened_role_check_constraint(self):
        db_path = temp_db_path()
        old_db_file = models.DB_FILE
        try:
            models.close_db()
            models.DB_FILE = db_path
            db = models.get_db()
            sql = db.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'users'"
            ).fetchone()[0]
            self.assertNotIn("preview", sql)
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute(
                    "INSERT INTO users (email, password, nickname, role) VALUES "
                    "('new-preview@example.com', 'hash', 'newpreviewer', 'preview')"
                )
        finally:
            models.close_db()
            models.DB_FILE = old_db_file
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(str(db_path) + suffix)
                except FileNotFoundError:
                    pass

    def test_existing_database_promotes_leftover_preview_accounts_without_rebuild(self):
        # No production database actually has 'preview' rows anymore (the role
        # was retired before any were created in practice), but this covers
        # the case defensively: an old database that still has one should get
        # it promoted to 'user' on next startup, without any table rebuild —
        # its original, more permissive CHECK constraint is left in place.
        db_path = temp_db_path()
        old_db_file = models.DB_FILE
        try:
            models.close_db()
            models.DB_FILE = db_path

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
            conn.commit()
            conn.close()

            db = models.get_db()

            role = db.execute("SELECT role FROM users WHERE id = 1").fetchone()[0]
            self.assertEqual(role, "user")

            # Same rows, same ids — no table rebuild happened.
            rows = db.execute("SELECT id, email, role FROM users ORDER BY id").fetchall()
            self.assertEqual(
                [tuple(r) for r in rows],
                [(1, "preview@example.com", "user"), (2, "admin@example.com", "admin")],
            )

            # The old, untightened constraint is still in place on this database.
            sql = db.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'users'"
            ).fetchone()[0]
            self.assertIn("preview", sql)
        finally:
            models.close_db()
            models.DB_FILE = old_db_file
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(str(db_path) + suffix)
                except FileNotFoundError:
                    pass


if __name__ == "__main__":
    unittest.main()
