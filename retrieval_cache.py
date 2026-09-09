"""法规检索路径的 SQLite 缓存。"""

from __future__ import annotations

import sqlite3
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CachedDocument:
    """缓存中已定位的一部法规及其条文页范围。"""

    doc_name: str
    pages: str


def normalize_question(question: str) -> str:
    """只统一格式差异，不改写问题语义。"""
    normalized = unicodedata.normalize("NFKC", question).strip()
    normalized = " ".join(normalized.split())
    return normalized.rstrip("。！？?!")


class RetrievalCache:
    """以归一化问题为键，缓存法规名称和条文页范围。"""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def get(self, question: str) -> list[CachedDocument]:
        """按问题查找已缓存的法规名称和条文页范围。"""
        # 查询结果按写入顺序返回，保持 PageIndex 原有的命中顺序。
        normalized_question = normalize_question(question)
        if not normalized_question:
            return []
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT doc_name, pages
                FROM retrieval_cache
                WHERE question = ?
                ORDER BY rowid
                """,
                (normalized_question,),
            ).fetchall()
        return [
            CachedDocument(doc_name=row["doc_name"], pages=row["pages"])
            for row in rows
        ]

    def put(self, question: str, documents: list[CachedDocument]) -> None:
        """替换保存问题对应的全部法规和条文页范围。"""
        # 一个问题可能对应多部法规，每部法规保存一个多页范围字符串。
        normalized_question = normalize_question(question)
        if not normalized_question or not documents:
            return
        unique_documents = list(dict.fromkeys(documents))
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM retrieval_cache WHERE question = ?",
                (normalized_question,),
            )
            connection.executemany(
                """
                INSERT INTO retrieval_cache(question, doc_name, pages)
                VALUES (?, ?, ?)
                """,
                [
                    (normalized_question, document.doc_name, document.pages)
                    for document in unique_documents
                ],
            )

    def _initialize(self) -> None:
        """创建全新的多法规、多页缓存表。"""
        with self._connect() as connection:
            connection.execute(
            """
            CREATE TABLE IF NOT EXISTS retrieval_cache (
                question TEXT NOT NULL,
                doc_name TEXT NOT NULL,
                pages TEXT NOT NULL,
                PRIMARY KEY (question, doc_name)
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
