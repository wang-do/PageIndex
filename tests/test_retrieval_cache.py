"""检索路径缓存的回归测试。"""

from retrieval_cache import CachedDocument, RetrievalCache, normalize_question


def test_normalize_question_only_removes_format_differences():
    """空白、全半角和句末标点不应影响缓存命中。"""
    assert normalize_question(" 栏杆净间距 有什么要求？\n") == "栏杆净间距 有什么要求"


def test_retrieval_cache_stores_multiple_documents_and_page_ranges(tmp_path):
    """缓存应保存并读取问题对应的多法规和多页范围。"""
    cache = RetrievalCache(tmp_path / "retrieval_cache.db")
    cache.put("旅馆公共走道净宽要求？", [
        CachedDocument(doc_name="GB55025-2022", pages="54,56-57"),
        CachedDocument(doc_name="GB55031-2022", pages="19"),
    ])

    results = cache.get(" 旅馆公共走道净宽要求？ ")

    assert results == [
        CachedDocument(doc_name="GB55025-2022", pages="54,56-57"),
        CachedDocument(doc_name="GB55031-2022", pages="19"),
    ]
