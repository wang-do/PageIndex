"""法规检索路径的 SQLite 缓存。"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import jieba.posseg

# 关键词串缓存键（第二级精确键）：jieba 切词 → 只留实词 → 词串全等匹配。
# 出现任何错命中时把开关置 False 即整体退回纯全文键。
KEYWORD_CACHE_ENABLED = True

USERDICT_PATH = Path(__file__).parent / "userdict.txt"
_KEEP_FLAG_PREFIX = "nvab"  # 保留名词/动词/形容词/区别词，虚词、代词、数词丢弃。


# 这个是是否缓存读入的文本
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

#################################缓存第一层2 关键词命中########################
_jieba_ready = False


def _ensure_jieba() -> None:
    """首次使用时加载 userdict（jieba 只初始化一次）。"""
    global _jieba_ready
    if not _jieba_ready:
        if USERDICT_PATH.exists():
            jieba.load_userdict(str(USERDICT_PATH))
        _jieba_ready = True


def keyword_key(question: str) -> str | None:
    """问题 → 关键词串缓存键；含数字或抽词过少时返回 None（只走全文键）。

    数字（数值/部位参数）一旦被切词折叠，"不小于0.26"和"0.22"就会撞键，
    因此含数字的问题一律不进关键词键；全文键不受影响（明文天然区分）。
    """
    if not KEYWORD_CACHE_ENABLED:
        return None
    _ensure_jieba()
    normalized = normalize_question(question)
    if re.search(r"[0-9０-９]", normalized):
        return None
    words = [
        word for word, flag in jieba.posseg.cut(normalized)
        if flag[0] in _KEEP_FLAG_PREFIX and len(word) >= 2
    ]
    if len(words) < 2:
        return None
    return f"K1|{'|'.join(words)}"
##############################################################################

class RetrievalCache:
    """以归一化问题为键，缓存法规名称和条文页范围。"""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def get(self, question: str) -> list[CachedDocument]:
        """按全文精确键查找已缓存的法规名称和条文页范围。"""
        normalized_question = normalize_question(question)
        if not normalized_question:
            return []
        return self._load(normalized_question)

    def get_by_keyword(self, question: str) -> list[CachedDocument]:
        """按关键词串键查找；含数字/抽词过少的问题返回空（自动跳过）。"""
        keyword = keyword_key(question)
        if not keyword:
            return []
        return self._load(keyword)

    def _load(self, key: str) -> list[CachedDocument]:
        """按缓存键读回法规名称和条文页范围，保持写入顺序。"""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT doc_name, pages
                FROM retrieval_cache
                WHERE question = ?
                ORDER BY rowid
                """,
                (key,),
            ).fetchall()
        return [
            CachedDocument(doc_name=row["doc_name"], pages=row["pages"])
            for row in rows
        ]

    def put(self, question: str, documents: list[CachedDocument]) -> None:
        """双写全文键和关键词串键（关键词键在含数字等问题上自动跳过）。"""
        if not documents:
            return
        normalized_question = normalize_question(question)
        if not normalized_question:
            return
        self._store(normalized_question, documents)
        keyword = keyword_key(question)
        if keyword:
            self._store(keyword, documents)

    def _store(self, key: str, documents: list[CachedDocument]) -> None:
        """整体替换一个键对应的全部法规和条文页范围。"""
        # 一个问题可能对应多部法规，每部法规保存一个多页范围字符串。
        unique_documents = list(dict.fromkeys(documents))
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM retrieval_cache WHERE question = ?",
                (key,),
            )
            connection.executemany(
                """
                INSERT INTO retrieval_cache(question, doc_name, pages)
                VALUES (?, ?, ?)
                """,
                [
                    (key, document.doc_name, document.pages)
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
