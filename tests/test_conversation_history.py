from ask_question import ask_question
from conversation_history import ConversationHistory
from retrieval_cache import RetrievalCache


def test_get_recent_returns_complete_turns_in_order(tmp_path):
    history = ConversationHistory(tmp_path / "conversation_history.db")
    history.append_turn("legal-chat", "第一个问题", "第一个回答")
    history.append_turn("legal-chat", "第二个问题", "第二个回答")

    assert history.get_recent("legal-chat", rounds=1) == [
        {"role": "user", "content": "第二个问题"},
        {"role": "assistant", "content": "第二个回答"},
    ]


def test_ask_question_passes_recent_messages_to_pageindex(tmp_path, monkeypatch):
    calls = []

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def responses(self, messages, **kwargs):
            calls.append(messages)
            return {
                "items": [],
                "output": [{"content": [{"text": "模拟回答"}]}],
            }

    monkeypatch.setattr("ask_question.PageIndexLocalClient", FakeClient)

    ask_question(None, "第一问", tmp_path, session_id="legal-chat")
    ask_question(None, "第二问", tmp_path, session_id="legal-chat")

    assert calls[1] == [
        {"role": "user", "content": "第一问"},
        {"role": "assistant", "content": "模拟回答"},
        {"role": "user", "content": "第二问"},
    ]


def test_only_complete_question_is_written_to_retrieval_cache(
    tmp_path, monkeypatch
):
    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def responses(self, messages, **kwargs):
            return {
                "items": [{
                    "type": "function_call",
                    "name": "get_page_content",
                    "arguments": '{"doc_name":"GB55025-2022","pages":"7"}',
                }],
                "output": [{"content": [{"text": "模拟回答"}]}],
            }

    monkeypatch.setattr("ask_question.PageIndexLocalClient", FakeClient)

    ask_question(None, "住宅卧室的净高要求是什么？", tmp_path, "legal-chat")
    ask_question(None, "这个要求可以降低吗？", tmp_path, "legal-chat")

    cache = RetrievalCache(tmp_path / "retrieval_cache.db")
    assert cache.get("住宅卧室的净高要求是什么？")
    assert cache.get("这个要求可以降低吗？") == []
