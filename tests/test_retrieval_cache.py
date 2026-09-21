"""检索路径缓存的回归测试。"""

import json

from retrieval_cache import RetrievalCache, is_complete_question, normalize_question


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


def test_retrieval_cache_stores_summary_and_references(tmp_path):
    """缓存应保存并读取要点总结与引用成品（引用直接回放，无重建）。"""
    cache = RetrievalCache(tmp_path / "retrieval_cache.db")
    refs_json = json.dumps([{"spec_no": "GB55031-2022", "clause_no": "5.3.7"}],
                           ensure_ascii=False)
    cache.put("旅馆公共走道净宽要求？", summary="净宽不应小于 1.3m", references_json=refs_json)

    entry = cache.get(" 旅馆公共走道净宽要求？ ")

    assert entry is not None
    summary, references = entry
    assert summary == "净宽不应小于 1.3m"
    assert references == [{"spec_no": "GB55031-2022", "clause_no": "5.3.7"}]
