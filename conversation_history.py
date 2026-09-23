"""保存命令行问答的最近对话消息。"""

from __future__ import annotations

import json
import sqlite3
import time
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

    def get_messages(self, session_id: str) -> list[dict[str, str]]:
        """读取一个会话的全部消息。"""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT role, content, cached, tokens, elapsed, "references", answer_format
                FROM conversation_messages
                WHERE session_id = ?
                ORDER BY id
                """,
                (session_id,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            if item.get("answer_format") is None:
                item.pop("answer_format")
            if item.get("references"):
                try:
                    item["references"] = json.loads(item["references"])
                except (TypeError, ValueError):
                    item["references"] = None
            result.append(item)
        return result

    def create_session(self, session_id: str) -> None:
        """创建空会话。"""
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO conversation_sessions(session_id, title, updated_at)
                VALUES (?, '新会话', ?)
                """,
                (session_id, self._now()),
            )

    def list_sessions(self) -> list[dict[str, str]]:
        """按最近使用时间返回会话列表。"""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT session_id, title
                FROM conversation_sessions
                ORDER BY updated_at DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def set_initial_title(self, session_id: str, question: str) -> str:
        """空会话收到第一问时，用问题生成标题。"""
        title = " ".join(question.split())[:30] or "新会话"
        with self._connect() as connection:
            row = connection.execute(
                "SELECT title FROM conversation_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO conversation_sessions(session_id, title, updated_at)
                    VALUES (?, ?, ?)
                    """,
                    (session_id, title, self._now()),
                )
                return title
            if row["title"] == "新会话":
                connection.execute(
                    """
                    UPDATE conversation_sessions
                    SET title = ?, updated_at = ?
                    WHERE session_id = ?
                    """,
                    (title, self._now(), session_id),
                )
                return title
            return row["title"]

    def rename_session(self, session_id: str, title: str) -> bool:
        """修改会话标题，返回会话是否存在。"""
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE conversation_sessions
                SET title = ?, updated_at = ?
                WHERE session_id = ?
                """,
                (title, self._now(), session_id),
            )
        return cursor.rowcount > 0

    def delete_session(self, session_id: str) -> bool:
        """删除会话及其全部消息，返回会话是否存在。"""
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM conversation_sessions WHERE session_id = ?",
                (session_id,),
            )
            connection.execute(
                "DELETE FROM conversation_messages WHERE session_id = ?",
                (session_id,),
            )
        return cursor.rowcount > 0

    def append_turn(
        self,
        session_id: str,
        question: str,
        answer: str,
        cached: bool | None = None,
        tokens: int | None = None,
        elapsed: float | None = None,
        references: list | None = None,
        answer_format: str | None = None,
    ) -> None:
        """保存一轮用户问题和 AI 回答（assistant 行附带缓存/token/耗时/引用）。"""
        references_json = (
            json.dumps(references, ensure_ascii=False) if references else None
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO conversation_sessions(session_id, title, updated_at)
                VALUES (?, ?, ?)
                """,
                (session_id, " ".join(question.split())[:30] or "新会话", self._now()),
            )
            connection.execute(
                """
                UPDATE conversation_sessions
                SET updated_at = ?
                WHERE session_id = ?
                """,
                (self._now(), session_id),
            )
            connection.executemany(
                """
                INSERT INTO conversation_messages(
                    session_id, role, content, cached, tokens, elapsed, "references", answer_format
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (session_id, "user", question, None, None, None, None, None),
                    (session_id, "assistant", answer, cached, tokens, elapsed,
                     references_json, answer_format),
                ],
            )

    def _initialize(self) -> None:
        """创建会话消息表及查询索引。"""
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_sessions (
                    session_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    cached INTEGER,
                    tokens INTEGER,
                    elapsed REAL,
                    "references" TEXT,
                    answer_format TEXT
                )
                """
            )
            columns = {row["name"] for row in connection.execute(
                "PRAGMA table_info(conversation_messages)")}
            if "answer_format" not in columns:
                connection.execute(
                    "ALTER TABLE conversation_messages ADD COLUMN answer_format TEXT")
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_conversation_messages_session
                ON conversation_messages(session_id, id)
                """
            )

    @staticmethod
    def _now() -> int:
        """返回用于会话排序的毫秒时间戳。"""
        return int(time.time() * 1000)

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
