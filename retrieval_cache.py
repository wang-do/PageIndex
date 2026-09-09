"""法规检索路径的 SQLite 缓存。"""

from __future__ import annotations

import sqlite3
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


# 出现这些词时，问题通常依赖前文，不能作为全局缓存键。
FOLLOW_UP_MARKERS = (
    "这个", "那个", "这种", "这样", "上述", "前述", "该要求", "该规定",
    "该条", "该项", "它", "呢",
)

# 这些短问句没有明确对象，只有结合上一轮才能理解。
SHORT_FOLLOW_UP_PREFIXES = (
    "那", "那么", "可以", "能否", "是否", "还要", "还需要", "具体",
)

# 法规问题中常见的独立查询意图和对象词。
QUESTION_INTENT_MARKERS = (
    "要求", "规定", "标准", "条件", "范围", "情形", "责任", "期限", "处罚",
    "尺寸", "净宽", "净高", "高度", "宽度", "间距", "面积", "数量", "定义",
    "适用", "应当", "不得", "是否需要", "是多少", "有哪些", "是什么",
    "能否", "是否", "可以",
)


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


def is_complete_question(question: str) -> bool:
    """用规则判断问题能否脱离对话独立理解。"""
    normalized = normalize_question(question)
    compact = normalized.replace(" ", "")
    if not compact:
        return False

    # 强指代或省略表达必须结合上一轮，禁止写入全局缓存。
    if any(marker in compact for marker in FOLLOW_UP_MARKERS):
        return False
    if len(compact) <= 8 and compact.startswith(SHORT_FOLLOW_UP_PREFIXES):
        return False

    # 至少包含查询意图，并有足够文本表达被查询的对象。
    has_intent = any(marker in compact for marker in QUESTION_INTENT_MARKERS)
    return has_intent and len(compact) >= 7


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
