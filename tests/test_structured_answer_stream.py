import json
import sqlite3

import ask_question as module
from conversation_history import ConversationHistory


def test_paragraph_parser_handles_arbitrary_token_boundaries():
    parser = module._ParagraphStreamParser()
    payload = (
        '{"type":"paragraph","text":"第一段","sources":'
        '[{"doc_name":"GB55025-2022","pages":"7"}]}\n'
        '{"type":"paragraph","text":"第二段","sources":[]}\n'
    )

    result = []
    for chunk in (payload[:9], payload[9:37], payload[37:68], payload[68:]):
        result.extend(parser.feed(chunk))

    assert result == [
        {"text": "第一段", "sources": [{"doc_name": "GB55025-2022", "pages": "7"}]},
        {"text": "第二段", "sources": []},
    ]


def test_paragraph_references_are_limited_to_pages_actually_read(monkeypatch):
    monkeypatch.setattr(module, "resolve_refs", lambda doc, pages: [{
        "spec_no": doc,
        "spec_name": doc,
        "clause_no": f"clause-{pages}",
        "page_pdf": int(pages),
        "bbox": None,
        "img_w": None,
        "img_h": None,
    }])
    items = [{
        "type": "function_call",
        "name": "get_page_content",
        "arguments": json.dumps({"doc_name": "A", "pages": "3"}),
    }]
    paragraphs = [{
        "text": "只有 A 的第 3 页可被引用",
        "sources": [
            {"doc_name": "A", "pages": "3"},
            {"doc_name": "A", "pages": "4"},
            {"doc_name": "B", "pages": "3"},
        ],
    }]

    enriched, references = module._attach_paragraph_references(paragraphs, items)

    assert [ref["clause_no"] for ref in references] == ["clause-3"]
    assert references[0]["paragraph_id"] == "p1"
    assert enriched[0]["references"] == references


def test_ask_question_streams_complete_json_paragraphs(tmp_path, monkeypatch):
    raw = (
        '{"type":"paragraph","text":"第一段","sources":'
        '[{"doc_name":"A","pages":"3"}]}\n'
        '{"type":"paragraph","text":"第二段","sources":[]}\n'
    )
    call = {
        "type": "function_call",
        "name": "get_page_content",
        "arguments": '{"doc_name":"A","pages":"3"}',
    }

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def responses(self, *args, **kwargs):
            yield {"type": "response.output_text.delta", "delta": raw[:40]}
            yield {"type": "response.output_text.delta", "delta": raw[40:]}
            yield {
                "type": "response.completed",
                "response": {
                    "output": [{"content": [{"text": raw}]}],
                    "items": [call],
                    "usage": {"input_tokens": 2, "output_tokens": 3},
                },
            }

    monkeypatch.setattr(module, "PageIndexLocalClient", FakeClient)
    monkeypatch.setattr(module, "resolve_refs", lambda doc, pages: [{
        "spec_no": "A-2026", "spec_name": "A", "clause_no": "1.2.3",
        "page_pdf": 8, "bbox": None, "img_w": None, "img_h": None,
    }])

    events = list(module.ask_question(["doc-a"], "测试问题", tmp_path))

    assert [(event["number"], event["text"])
            for event in events if event["type"] == "paragraph"] == [
        (1, "第一段"), (2, "第二段")
    ]
    done = events[-1]
    assert done["answer"] == "第一段\n\n第二段"
    assert [paragraph["number"] for paragraph in done["paragraphs"]] == [1, 2]
    assert done["paragraphs"][0]["references"][0]["paragraph_id"] == "p1"
    assert done["paragraphs"][1]["references"] == []

    history = ConversationHistory(tmp_path / "conversation_history.db")
    assert history.get_messages("default")[-1]["answer_format"] == "paragraphs-v1"


def test_existing_history_database_accepts_numbered_answers(tmp_path):
    database = tmp_path / "history.db"
    with sqlite3.connect(database) as connection:
        connection.execute("""
            CREATE TABLE conversation_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                cached INTEGER,
                tokens INTEGER,
                elapsed REAL,
                "references" TEXT
            )
        """)

    history = ConversationHistory(database)
    history.append_turn("session", "问题", "第一段\n\n第二段",
                        answer_format="paragraphs-v1")

    assert history.get_messages("session")[-1]["answer_format"] == "paragraphs-v1"
