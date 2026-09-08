"""PageIndex 结构 JSON 导入的本地回归测试。"""

import json

from pageindex import PageIndexLocalClient


def test_imported_structure_uses_existing_local_retrieval_surfaces(tmp_path):
    """导入后的文档可由现有树和页面读取接口访问。"""
    source = tmp_path / "structure.json"
    source.write_text(json.dumps({
        "doc_name": "demo.pdf",
        "doc_description": "测试文档",
        "structure": [{
            "title": "第 1 章 栏杆",
            "node_id": "0000",
            "start_index": 1,
            "end_index": 1,
            "summary": "栏杆净间距要求。",
        }],
        "pages": [{
            "page_index": 1,
            "markdown": "栏杆垂直杆件间的净间距不应大于 0.11m。",
        }],
    }, ensure_ascii=False), encoding="utf-8")
    client = PageIndexLocalClient(
        model="deepseek/deepseek-chat",
        storage_path=tmp_path / "store",
    )

    doc_id = client.submit_structure_json(str(source))["doc_id"]

    structure = client.get_document_structure(doc_id)
    pages = client.get_page_content(doc_id, "1")
    assert structure[0]["node_id"] == "0000"
    assert structure[0]["summary"] == "栏杆净间距要求。"
    assert pages == [{
        "page_index": 1,
        "markdown": "栏杆垂直杆件间的净间距不应大于 0.11m。",
    }]
