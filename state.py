"""Small transactional journal: IDs/receipts only, never tokens or message bodies."""

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path


def cursor_number(value: str) -> int:
    if (
        not isinstance(value, str)
        or not value.isascii()
        or not value.isdecimal()
        or len(value) > 19
        or int(value) > 9223372036854775807
    ):
        raise ValueError("invalid_cursor")
    return int(value)


def clean_sticker_caption(value: str | None) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = "".join(" " if ord(c) < 32 or ord(c) == 127 else c for c in value)
    return " ".join(cleaned.split())[:160]


class StateIdentityError(RuntimeError):
    """Stored identity changed; preserve evidence and require operator inspection."""


class StateStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        path.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS checkpoint (id INTEGER PRIMARY KEY, cursor TEXT NOT NULL);
            INSERT OR IGNORE INTO checkpoint VALUES (1, '0');
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS recalls (
                thread TEXT NOT NULL, server_id TEXT NOT NULL, PRIMARY KEY(thread, server_id));
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY, thread TEXT NOT NULL, server_id TEXT, status TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS message_days (
                message_id TEXT PRIMARY KEY, day TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS message_days_day ON message_days(day);
        """)
        with self.db:
            # 排队中的消息不因重启而作废：宿主可能还没轮到回复它们，
            # 直接判成 needs_review 会让这些消息永远得不到回复。
            # 但超过一天仍未回复的排队消息已经失去意义，标记为不再回复，
            # 避免它们无限累积并干扰后续匹配。
            self.db.execute(
                "UPDATE messages SET status='no_reply' WHERE status='queued' AND id IN "
                "(SELECT message_id FROM message_days WHERE day < ?)",
                ((datetime.now().astimezone() - timedelta(days=1)).strftime("%Y-%m-%d"),),
            )

    def bind_server(self, server_id: str):
        if not isinstance(server_id, str) or not server_id:
            raise StateIdentityError("Native server identity unavailable; state retained")
        row = self.db.execute("SELECT value FROM settings WHERE key='server_id'").fetchone()
        if row and row[0] != server_id:
            raise StateIdentityError("Native server identity changed; state retained for review")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO settings VALUES ('server_id', ?)", (server_id,))

    def rebind_server(self, server_id: str):
        """服务端身份变化后把状态库重新绑定到新实例。

        Args:
            server_id: 新的 Native ``serverId``。
        """

        if not isinstance(server_id, str) or not server_id:
            raise StateIdentityError("Native server identity unavailable; state retained")
        with self.db:
            self.db.execute(
                "INSERT INTO settings VALUES ('server_id', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (server_id,),
            )

    @property
    def cursor(self) -> str:
        return self.db.execute("SELECT cursor FROM checkpoint WHERE id=1").fetchone()[0]

    def _advance(self, cursor: str):
        if cursor_number(cursor) > cursor_number(self.cursor):
            self.db.execute("UPDATE checkpoint SET cursor=? WHERE id=1", (cursor,))

    def advance(self, cursor: str):
        with self.db:
            self._advance(cursor)

    def advance_reset(self, cursor: str):
        """强制把游标设为指定值，用于服务端事件日志重置后的回退。

        Args:
            cursor: 新的游标值。
        """

        cursor_number(cursor)
        with self.db:
            self.db.execute("UPDATE checkpoint SET cursor=? WHERE id=1", (cursor,))

    def accept(
        self,
        cursor: str,
        message_id: str,
        thread: str,
        server_id: str | None,
        status: str,
        *,
        received: bool = True,
    ) -> bool:
        existing = self.db.execute(
            "SELECT thread, server_id FROM messages WHERE id=?", (message_id,)
        ).fetchone()
        if existing and (existing[0] != thread or existing[1] != server_id):
            self.mark_message(message_id, "needs_review")
            raise StateIdentityError("Native message identity reused; cursor retained for review")
        with self.db:
            recalled = self.db.execute(
                "SELECT 1 FROM recalls WHERE thread=? AND server_id=?", (thread, server_id)
            ).fetchone()
            result = self.db.execute(
                "INSERT OR IGNORE INTO messages VALUES (?, ?, ?, ?)",
                (message_id, thread, server_id, "recalled" if recalled else status),
            )
            if result.rowcount == 1 and received:
                self.db.execute(
                    "INSERT INTO message_days VALUES (?, ?)",
                    (message_id, datetime.now().astimezone().date().isoformat()),
                )
            self._advance(cursor)
            return result.rowcount == 1 and not recalled

    def message_status(self, message_id: str) -> str | None:
        row = self.db.execute("SELECT status FROM messages WHERE id=?", (message_id,)).fetchone()
        return row[0] if row else None

    def find_message_id(self, thread: str, server_id: str) -> str | None:
        rows = self.db.execute(
            "SELECT id FROM messages WHERE thread=? AND server_id=? LIMIT 2", (thread, server_id)
        ).fetchall()
        return rows[0][0] if len(rows) == 1 else None

    def latest_pending_message(self, account: str, thread: str) -> str | None:
        """返回指定会话中最近一条等待回复的入站消息。

        Args:
            account: Native 账号 ID。
            thread: 会话 ID，群聊为 ``@chatroom`` 结尾的 threadId。

        Returns:
            str | None: 待回复消息 ID；没有匹配时返回 ``None``。
        """

        prefix = f"{account}:{thread}:"
        row = self.db.execute(
            "SELECT id FROM messages WHERE status='queued' AND id LIKE ? ORDER BY rowid DESC LIMIT 1",
            (prefix + "%",),
        ).fetchone()
        return row[0] if row else None

    def mark_message(self, message_id: str, status: str):
        with self.db:
            self.db.execute(
                "UPDATE messages SET status=CASE "
                "WHEN status='partial' THEN 'partial' "
                "WHEN status='replied' AND ? IN ('failed','needs_review') THEN 'partial' "
                "WHEN status IN ('failed','needs_review') AND ?='replied' THEN 'partial' "
                "ELSE ? END WHERE id=? AND status!='recalled'",
                (status, status, status, message_id),
            )

    def recall(self, thread: str, server_id: str | None, message_id: str | None):
        with self.db:
            if server_id:
                self.db.execute("INSERT OR IGNORE INTO recalls VALUES (?, ?)", (thread, server_id))
            self.db.execute(
                "UPDATE messages SET status='recalled' WHERE thread=? AND "
                "((? IS NOT NULL AND server_id=?) OR (? IS NULL AND id=?))",
                (thread, server_id, server_id, server_id, message_id),
            )
            if message_id:
                self.db.execute(
                    "INSERT OR IGNORE INTO messages VALUES (?, ?, ?, 'recalled')",
                    (message_id, thread, server_id),
                )

    def close(self):
        self.db.close()
