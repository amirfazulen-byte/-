"""SQLite-хранилище «ОтветБота». Время — UNIX-секунды в UTC."""

import calendar
import json
import sqlite3
import time
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

SCHEMA = """
CREATE TABLE IF NOT EXISTS sellers (
    id INTEGER PRIMARY KEY,
    tg_id INTEGER UNIQUE NOT NULL,
    name TEXT NOT NULL,
    wb_token_enc BLOB,
    token_ok INTEGER NOT NULL DEFAULT 0,
    style_examples TEXT NOT NULL DEFAULT '',
    autopublish_5 INTEGER NOT NULL DEFAULT 0,  -- публиковать 5★ без рисков сразу
    paused INTEGER NOT NULL DEFAULT 0,
    monthly_limit INTEGER NOT NULL DEFAULT 100,
    awaiting TEXT,                              -- что ждём от продавца: token | style | edit:<item_id>
    last_poll_at INTEGER,
    last_digest_at INTEGER,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    seller_id INTEGER NOT NULL REFERENCES sellers(id),
    kind TEXT NOT NULL,                 -- feedback | question
    wb_id TEXT NOT NULL,
    nm_id INTEGER,
    product_name TEXT,
    rating INTEGER,
    text TEXT,
    pros TEXT,
    cons TEXT,
    size TEXT,
    created_wb TEXT,
    draft TEXT,
    needs_seller INTEGER,
    reason TEXT,
    -- new -> drafted -> published | skipped ; draft_failed / publish_failed — повтор на следующем цикле
    status TEXT NOT NULL DEFAULT 'new',
    tg_message_id INTEGER,
    published_text TEXT,
    published_at INTEGER,
    published_by TEXT,                  -- seller | auto
    error TEXT,
    created_at INTEGER NOT NULL,
    UNIQUE (seller_id, kind, wb_id)
);

CREATE TABLE IF NOT EXISTS cards (
    seller_id INTEGER NOT NULL,
    nm_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    fetched_at INTEGER NOT NULL,
    PRIMARY KEY (seller_id, nm_id)
);

-- Журнал событий: отладка «почему не опубликовалось» и метрики пилота.
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    ts INTEGER NOT NULL,
    seller_id INTEGER,
    item_id INTEGER,
    kind TEXT NOT NULL,
    payload TEXT
);

CREATE INDEX IF NOT EXISTS idx_items_status ON items(seller_id, status);
"""


def now_ts() -> int:
    return int(time.time())


class TokenBox:
    """Шифрование токенов WB. Ключ хранится вне базы (переменная окружения)."""

    def __init__(self, key: str):
        self._fernet = Fernet(key.encode())

    def seal(self, token: str) -> bytes:
        return self._fernet.encrypt(token.encode())

    def open(self, blob: bytes) -> str | None:
        try:
            return self._fernet.decrypt(blob).decode()
        except InvalidToken:
            return None


class DB:
    def __init__(self, path: str | Path):
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def log(self, kind: str, *, seller_id=None, item_id=None, **payload) -> None:
        self.conn.execute(
            "INSERT INTO events (ts, seller_id, item_id, kind, payload) VALUES (?, ?, ?, ?, ?)",
            (now_ts(), seller_id, item_id, kind, json.dumps(payload, ensure_ascii=False) if payload else None),
        )
        self.conn.commit()

    # --- продавцы ---

    def seller_by_tg(self, tg_id: int):
        return self.conn.execute("SELECT * FROM sellers WHERE tg_id = ?", (tg_id,)).fetchone()

    def seller(self, seller_id: int):
        return self.conn.execute("SELECT * FROM sellers WHERE id = ?", (seller_id,)).fetchone()

    def active_sellers(self):
        return self.conn.execute("SELECT * FROM sellers WHERE token_ok = 1 AND paused = 0").fetchall()

    def all_sellers(self):
        return self.conn.execute("SELECT * FROM sellers ORDER BY id").fetchall()

    def add_seller(self, tg_id: int, name: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO sellers (tg_id, name, created_at, awaiting) VALUES (?, ?, ?, 'token')", (tg_id, name, now_ts())
        )
        self.conn.commit()
        self.log("seller_joined", seller_id=cur.lastrowid)
        return cur.lastrowid

    def update_seller(self, seller_id: int, **fields) -> None:
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE sellers SET {cols} WHERE id = ?", (*fields.values(), seller_id))
        self.conn.commit()

    def answers_this_month(self, seller_id: int, now: int) -> int:
        t = time.gmtime(now)
        month_start = calendar.timegm((t.tm_year, t.tm_mon, 1, 0, 0, 0, 0, 0, 0))
        return self.conn.execute(
            "SELECT COUNT(*) FROM items WHERE seller_id = ? AND status = 'published' AND published_at >= ?",
            (seller_id, month_start),
        ).fetchone()[0]

    # --- отзывы и вопросы ---

    def add_item(self, seller_id: int, item) -> int | None:
        """Сохраняет новый отзыв/вопрос. Возвращает id или None, если он уже есть."""
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO items
               (seller_id, kind, wb_id, nm_id, product_name, rating, text, pros, cons, size, created_wb, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (seller_id, item.kind, item.wb_id, item.product.nm_id, item.product.name, item.rating,
             item.text, item.pros, item.cons, item.product.size, item.created_at, now_ts()),
        )
        self.conn.commit()
        return cur.lastrowid if cur.rowcount else None

    def item(self, item_id: int):
        return self.conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()

    def items_with_status(self, seller_id: int, *statuses: str):
        q = f"SELECT * FROM items WHERE seller_id = ? AND status IN ({','.join('?' * len(statuses))}) ORDER BY id"
        return self.conn.execute(q, (seller_id, *statuses)).fetchall()

    def update_item(self, item_id: int, **fields) -> None:
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE items SET {cols} WHERE id = ?", (*fields.values(), item_id))
        self.conn.commit()

    def items_since(self, seller_id: int, since: int):
        return self.conn.execute(
            "SELECT * FROM items WHERE seller_id = ? AND created_at >= ?", (seller_id, since)
        ).fetchall()

    # --- кэш карточек ---

    def cached_card(self, seller_id: int, nm_id: int, max_age: int):
        row = self.conn.execute(
            "SELECT text, fetched_at FROM cards WHERE seller_id = ? AND nm_id = ?", (seller_id, nm_id)
        ).fetchone()
        if row and now_ts() - row["fetched_at"] < max_age:
            return row["text"]
        return None

    def save_card(self, seller_id: int, nm_id: int, text: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO cards (seller_id, nm_id, text, fetched_at) VALUES (?, ?, ?, ?)",
            (seller_id, nm_id, text, now_ts()),
        )
        self.conn.commit()
