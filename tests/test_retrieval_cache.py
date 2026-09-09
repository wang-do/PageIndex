"""检索路径缓存的回归测试。"""

from retrieval_cache import (
    CachedDocument,
    RetrievalCache,
    is_complete_question,
    normalize_question,
)


def test_is_complete_question_uses_conservative_rules():
    assert is_complete_question("住宅卧室的净高要求是什么？")
    assert is_complete_question("第十条规定了哪些责任？")
    assert is_complete_question("住宅楼梯能否采用扇形踏步？")
    assert not is_complete_question("双面布房呢？")
    assert not is_complete_question("这个要求可以降低吗？")
    assert not is_complete_question("可以降低吗？")


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
