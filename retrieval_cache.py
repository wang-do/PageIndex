"""法规检索结果的 SQLite 缓存：每个问题一行，存要点总结与引用成品。"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import jieba.posseg

# 关键词串缓存键（第二级精确键）：jieba 切词 → 只留实词 → 词串全等匹配。
# 出现任何错命中时把开关置 False 即整体退回纯全文键。
KEYWORD_CACHE_ENABLED = True

USERDICT_PATH = Path(__file__).parent / "userdict.txt"
_KEEP_FLAG_PREFIX = "nvab"  # 保留名词/动词/形容词/区别词，虚词、代词、数词丢弃。

# 缓存键数上限（LRU）：超过即按最久未使用整键淘汰；命中会自动续命。
MAX_KEY_ENTRIES = 50

# 纯疑问形式词，不参与键区分（"X有哪些"和"X"是同一意图）。
_DROP_WORDS = {
    "有哪些", "哪些", "什么", "是多少", "是什么", "多少",
    "啥", "怎么", "怎样", "如何", "请问", "一下",
}

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
        and word not in _DROP_WORDS
    ]
    if len(words) < 2:
        return None
    return f"K1|{'|'.join(words)}"


class RetrievalCache:
    """以归一化问题为键，每问题一行缓存要点总结与引用成品。

    命中判定：get/get_by_keyword 返回非 None 即命中；
    引用回放：直接返回存好的 references 成品，不做任何重建。
    """

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def get(self, question: str) -> tuple[str | None, list[dict] | None] | None:
        """按全文精确键取 (summary, references)；无缓存返回 None。"""
        normalized = normalize_question(question)
        if not normalized:
            return None
        return self._load(normalized)

    def get_by_keyword(self, question: str) -> tuple[str | None, list[dict] | None] | None:
        """按关键词串键取；含数字/抽词过少的问题返回 None（自动跳过）。"""
        keyword = keyword_key(question)
        if not keyword:
            return None
        return self._load(keyword)

    def _load(self, key: str) -> tuple[str | None, list[dict] | None] | None:
        """读一个键的 (summary, references) 并做 LRU 续命。"""
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT summary, references_json
                FROM retrieval_cache
                WHERE question = ?
                """,
                (key,),
            ).fetchone()
            if row is None:
                return None
            # LRU 续命：删除后重插，rowid 变为最新。
            connection.execute(
                "DELETE FROM retrieval_cache WHERE question = ?", (key,))
            connection.execute(
                """
                INSERT INTO retrieval_cache(question, summary, references_json)
                VALUES (?, ?, ?)
                """,
                (key, row["summary"], row["references_json"]),
            )
        try:
            references = json.loads(row["references_json"]) if row["references_json"] else None
        except (TypeError, ValueError):
            references = None
        return row["summary"], references

    def put(self, question: str, summary: str | None = None,
            references_json: str | None = None) -> None:
        """双写全文键和关键词串键（关键词键在含数字等问题上自动跳过）。"""
        normalized_question = normalize_question(question)
        if not normalized_question:
            return
        self._store(normalized_question, summary, references_json)
        keyword = keyword_key(question)
        if keyword:
            self._store(keyword, summary, references_json)

    def _store(self, key: str, summary: str | None,
               references_json: str | None) -> None:
        """整体替换一个键的内容，并维持 LRU 上限。"""
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM retrieval_cache WHERE question = ?", (key,))
            connection.execute(
                """
                INSERT INTO retrieval_cache(question, summary, references_json)
                VALUES (?, ?, ?)
                """,
                (key, summary, references_json),
            )
            self._evict_oldest(connection)

    def _evict_oldest(self, connection: sqlite3.Connection) -> None:
        """键数超过上限时，按 rowid 从老到新淘汰（LRU 语义，
        命中的键已在 _load 中续到最新）。"""
        keys = connection.execute(
            "SELECT question FROM retrieval_cache ORDER BY rowid"
        ).fetchall()
        overflow = len(keys) - MAX_KEY_ENTRIES
        if overflow <= 0:
            return
        connection.executemany(
            "DELETE FROM retrieval_cache WHERE question = ?",
            [(row["question"],) for row in keys[:overflow]],
        )

    def _initialize(self) -> None:
        """创建单行缓存表；旧的多行结构（含 doc_name 列）自动重建。"""
        with self._connect() as connection:
            columns = [row["name"] for row in connection.execute(
                "PRAGMA table_info(retrieval_cache)")]
            if columns and "doc_name" in columns:
                connection.execute("DROP TABLE retrieval_cache")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS retrieval_cache (
                    question TEXT PRIMARY KEY,
                    summary TEXT,
                    references_json TEXT
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
