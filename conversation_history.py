"""保存命令行问答的最近对话消息。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class ConversationHistory:
    """按会话保存 user 和 assistant 的纯文本消息。"""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def get_recent(self, session_id: str, rounds: int) -> list[dict[str, str]]:
        """读取会话最近若干轮消息，按原始对话顺序返回。"""
        if rounds <= 0:
            return []
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT role, content
                FROM conversation_messages
                WHERE session_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (session_id, rounds * 2),
            ).fetchall()
        return [
            {"role": row["role"], "content": row["content"]}
            for row in reversed(rows)
        ]

    def append_turn(self, session_id: str, question: str, answer: str) -> None:
        """保存一轮用户问题和 AI 回答。"""
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO conversation_messages(session_id, role, content)
                VALUES (?, ?, ?)
                """,
                [
                    (session_id, "user", question),
                    (session_id, "assistant", answer),
                ],
            )

    def _initialize(self) -> None:
        """创建会话消息表及查询索引。"""
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                    content TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_conversation_messages_session
                ON conversation_messages(session_id, id)
                """
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """创建连接并确保事务完整提交或回滚。"""
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
