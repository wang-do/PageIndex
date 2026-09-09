"""法规检索路径的 SQLite 缓存。"""

from __future__ import annotations

import sqlite3
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CachedPage:
    """缓存中已定位的单个法规条文页。"""

    doc_id: str
    page_index: int


def normalize_question(question: str) -> str:
    """只统一格式差异，不改写问题语义。"""
    normalized = unicodedata.normalize("NFKC", question).strip()
    normalized = " ".join(normalized.split())
    return normalized.rstrip("。！？?!")


class RetrievalCache:
    """以归一化问题为键，缓存一个或多个法规条文逻辑页。"""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def get(self, question: str) -> list[CachedPage]:
        """按问题查找已缓存的全部条文定位。"""
        
        # 查询结果按写入顺序返回，保持 PageIndex 原有的命中顺序。
        normalized_question = normalize_question(question)
        if not normalized_question:
            return []
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT doc_id, page_index
                FROM retrieval_cache
                WHERE question = ?
                ORDER BY rowid
                """,
                (normalized_question,),
            ).fetchall()
        return [
            CachedPage(doc_id=row["doc_id"], page_index=row["page_index"])
            for row in rows
        ]

    def put(self, question: str, pages: list[CachedPage]) -> None:
        """替换保存问题对应的全部法规和条文逻辑页。"""
        # 一个问题可能对应多个法规和条文，因此按复合主键批量保存。
        normalized_question = normalize_question(question)
        if not normalized_question or not pages:
            return
        unique_pages = list(dict.fromkeys(pages))
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM retrieval_cache WHERE question = ?",
                (normalized_question,),
            )
            connection.executemany(
                """
                INSERT INTO retrieval_cache(question, doc_id, page_index)
                VALUES (?, ?, ?)
                """,
                [
                    (normalized_question, page.doc_id, page.page_index)
                    for page in unique_pages
                ],
            )

    def _initialize(self) -> None:
        """创建全新的多条文缓存表。"""
        with self._connect() as connection:
            connection.execute(
            """
            CREATE TABLE IF NOT EXISTS retrieval_cache (
                question TEXT NOT NULL,
                doc_id TEXT NOT NULL,
                page_index INTEGER NOT NULL CHECK (page_index > 0),
                PRIMARY KEY (question, doc_id, page_index)
            )
            """
        )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """创建连接并确保完成操作后关闭，避免 SQLite 文件锁。"""
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
