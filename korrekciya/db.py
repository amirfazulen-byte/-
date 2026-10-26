"""SQLite-хранилище. Время везде — UNIX-секунды в UTC."""

import json
import secrets
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS masters (
    id INTEGER PRIMARY KEY,
    tg_id INTEGER UNIQUE NOT NULL,
    name TEXT NOT NULL,
    tz TEXT NOT NULL DEFAULT 'Europe/Moscow',
    interval_days INTEGER NOT NULL DEFAULT 21,
    last_report_at INTEGER,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS clients (
    id INTEGER PRIMARY KEY,
    master_id INTEGER NOT NULL REFERENCES masters(id),
    name TEXT NOT NULL,
    phone TEXT,
    tg_id INTEGER,
    link_token TEXT UNIQUE NOT NULL,
    invite_consent INTEGER,          -- NULL: не спрашивали, 1: да, 0: нет
    consent_at INTEGER,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS visits (
    id INTEGER PRIMARY KEY,
    master_id INTEGER NOT NULL REFERENCES masters(id),
    client_id INTEGER NOT NULL REFERENCES clients(id),
    starts_at INTEGER NOT NULL,
    service TEXT NOT NULL,
    -- scheduled -> confirmed | reschedule | cancelled ; после визита: attended | no_show
    status TEXT NOT NULL DEFAULT 'scheduled',
    reminder_sent_at INTEGER,
    reminder_channel TEXT,           -- telegram | sms | manual | skipped
    master_alert_sent_at INTEGER,
    outcome_asked_at INTEGER,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS invites (
    id INTEGER PRIMARY KEY,
    master_id INTEGER NOT NULL REFERENCES masters(id),
    client_id INTEGER NOT NULL REFERENCES clients(id),
    visit_id INTEGER REFERENCES visits(id),
    due_at INTEGER NOT NULL,
    sent_at INTEGER,
    response TEXT                    -- book | later | stop
);

-- Журнал всех событий: по нему отлаживаем «не пришло» и считаем метрики пилота.
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    ts INTEGER NOT NULL,
    master_id INTEGER,
    visit_id INTEGER,
    client_id INTEGER,
    kind TEXT NOT NULL,
    payload TEXT
);

CREATE INDEX IF NOT EXISTS idx_visits_due ON visits(status, starts_at);
CREATE INDEX IF NOT EXISTS idx_invites_due ON invites(sent_at, due_at);
CREATE INDEX IF NOT EXISTS idx_clients_master ON clients(master_id, name);
"""


def now_ts() -> int:
    return int(time.time())


class DB:
    def __init__(self, path: str | Path):
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # --- события ---

    def log(self, kind: str, *, master_id=None, visit_id=None, client_id=None, ts=None, **payload) -> None:
        self.conn.execute(
            "INSERT INTO events (ts, master_id, visit_id, client_id, kind, payload) VALUES (?, ?, ?, ?, ?, ?)",
            (ts or now_ts(), master_id, visit_id, client_id, kind,
             json.dumps(payload, ensure_ascii=False) if payload else None),
        )
        self.conn.commit()

    # --- мастера ---

    def get_master_by_tg(self, tg_id: int):
        return self.conn.execute("SELECT * FROM masters WHERE tg_id = ?", (tg_id,)).fetchone()

    def get_master(self, master_id: int):
        return self.conn.execute("SELECT * FROM masters WHERE id = ?", (master_id,)).fetchone()

    def all_masters(self):
        return self.conn.execute("SELECT * FROM masters ORDER BY id").fetchall()

    def add_master(self, tg_id: int, name: str, tz: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO masters (tg_id, name, tz, created_at) VALUES (?, ?, ?, ?)",
            (tg_id, name, tz, now_ts()),
        )
        self.conn.commit()
        self.log("master_joined", master_id=cur.lastrowid)
        return cur.lastrowid

    def update_master(self, master_id: int, **fields) -> None:
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE masters SET {cols} WHERE id = ?", (*fields.values(), master_id))
        self.conn.commit()

    # --- клиентки ---

    def get_client(self, client_id: int):
        return self.conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()

    def get_client_by_token(self, token: str):
        return self.conn.execute("SELECT * FROM clients WHERE link_token = ?", (token,)).fetchone()

    def find_client(self, master_id: int, name: str):
        # lower() в SQLite не знает кириллицу, поэтому сравниваем в Python.
        key = name.casefold().replace("ё", "е")
        rows = self.conn.execute(
            "SELECT * FROM clients WHERE master_id = ? ORDER BY id DESC", (master_id,)
        ).fetchall()
        return next((r for r in rows if r["name"].casefold().replace("ё", "е") == key), None)

    def clients_of(self, master_id: int):
        return self.conn.execute(
            "SELECT * FROM clients WHERE master_id = ? ORDER BY name", (master_id,)
        ).fetchall()

    def add_client(self, master_id: int, name: str, phone: str | None) -> int:
        cur = self.conn.execute(
            "INSERT INTO clients (master_id, name, phone, link_token, created_at) VALUES (?, ?, ?, ?, ?)",
            (master_id, name, phone, secrets.token_urlsafe(9), now_ts()),
        )
        self.conn.commit()
        return cur.lastrowid

    def update_client(self, client_id: int, **fields) -> None:
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE clients SET {cols} WHERE id = ?", (*fields.values(), client_id))
        self.conn.commit()

    def forget_client(self, client_id: int) -> None:
        """Удаление данных клиентки по её запросу: обезличиваем, историю визитов оставляем для статистики."""
        self.conn.execute(
            "UPDATE clients SET name = 'удалено', phone = NULL, tg_id = NULL, invite_consent = 0 WHERE id = ?",
            (client_id,),
        )
        self.conn.execute("UPDATE invites SET response = 'stop' WHERE client_id = ? AND sent_at IS NULL", (client_id,))
        self.conn.commit()

    # --- визиты ---

    def add_visit(self, master_id: int, client_id: int, starts_at: int, service: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO visits (master_id, client_id, starts_at, service, created_at) VALUES (?, ?, ?, ?, ?)",
            (master_id, client_id, starts_at, service, now_ts()),
        )
        self.conn.commit()
        return cur.lastrowid

    def get_visit(self, visit_id: int):
        return self.conn.execute("SELECT * FROM visits WHERE id = ?", (visit_id,)).fetchone()

    def update_visit(self, visit_id: int, **fields) -> None:
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE visits SET {cols} WHERE id = ?", (*fields.values(), visit_id))
        self.conn.commit()

    def upcoming_visits(self, master_id: int, since: int, until: int):
        return self.conn.execute(
            """SELECT v.*, c.name AS client_name, c.tg_id AS client_tg
               FROM visits v JOIN clients c ON c.id = v.client_id
               WHERE v.master_id = ? AND v.starts_at BETWEEN ? AND ?
                 AND v.status IN ('scheduled', 'confirmed', 'reschedule')
               ORDER BY v.starts_at""",
            (master_id, since, until),
        ).fetchall()

    def next_visit_of_client(self, client_id: int, since: int):
        return self.conn.execute(
            """SELECT * FROM visits WHERE client_id = ? AND starts_at > ?
               AND status IN ('scheduled', 'confirmed') ORDER BY starts_at LIMIT 1""",
            (client_id, since),
        ).fetchone()

    def visits_needing_reminder(self, now: int, window: int):
        return self.conn.execute(
            """SELECT * FROM visits WHERE status = 'scheduled' AND reminder_sent_at IS NULL
               AND starts_at > ? AND starts_at <= ?""",
            (now, now + window),
        ).fetchall()

    def visits_without_answer(self):
        return self.conn.execute(
            """SELECT * FROM visits WHERE status = 'scheduled' AND reminder_sent_at IS NOT NULL
               AND reminder_channel != 'skipped' AND master_alert_sent_at IS NULL"""
        ).fetchall()

    def visits_needing_outcome(self, before: int):
        return self.conn.execute(
            """SELECT * FROM visits WHERE status IN ('scheduled', 'confirmed', 'reschedule')
               AND outcome_asked_at IS NULL AND starts_at <= ?""",
            (before,),
        ).fetchall()

    def visits_between(self, master_id: int, since: int, until: int):
        return self.conn.execute(
            "SELECT * FROM visits WHERE master_id = ? AND starts_at >= ? AND starts_at < ?",
            (master_id, since, until),
        ).fetchall()

    # --- приглашения ---

    def add_invite(self, master_id: int, client_id: int, visit_id: int | None, due_at: int) -> int:
        # Одно ожидающее приглашение на клиентку: новое заменяет старое.
        self.conn.execute(
            "DELETE FROM invites WHERE client_id = ? AND sent_at IS NULL AND response IS NULL", (client_id,)
        )
        cur = self.conn.execute(
            "INSERT INTO invites (master_id, client_id, visit_id, due_at) VALUES (?, ?, ?, ?)",
            (master_id, client_id, visit_id, due_at),
        )
        self.conn.commit()
        return cur.lastrowid

    def get_invite(self, invite_id: int):
        return self.conn.execute("SELECT * FROM invites WHERE id = ?", (invite_id,)).fetchone()

    def update_invite(self, invite_id: int, **fields) -> None:
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE invites SET {cols} WHERE id = ?", (*fields.values(), invite_id))
        self.conn.commit()

    def due_invites(self, now: int):
        return self.conn.execute(
            "SELECT * FROM invites WHERE sent_at IS NULL AND response IS NULL AND due_at <= ?", (now,)
        ).fetchall()

    def cancel_pending_invites(self, client_id: int) -> None:
        self.conn.execute(
            "DELETE FROM invites WHERE client_id = ? AND sent_at IS NULL AND response IS NULL", (client_id,)
        )
        self.conn.commit()

    def invites_between(self, master_id: int, since: int, until: int):
        return self.conn.execute(
            "SELECT * FROM invites WHERE master_id = ? AND sent_at >= ? AND sent_at < ?",
            (master_id, since, until),
        ).fetchall()
