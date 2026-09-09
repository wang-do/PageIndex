"""使用已有的 PageIndex 索引提问。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Optional

from pageindex import PageIndexLocalClient
from retrieval_cache import CachedDocument, RetrievalCache


DEFAULT_QUESTION = "宿舍、旅馆选址有什么规定？GB55025-2022"


def ask_question(
    doc_ids: Optional[list[str]], question: str, storage_path: Path
) -> str:
    """优先复用条文页缓存；未命中时调用 PageIndex 原生检索。"""
    client = PageIndexLocalClient(
        index_model="deepseek/deepseek-chat",
        chat_model="deepseek-chat",
        storage_path=str(storage_path),
    )
    cache = RetrievalCache(storage_path / "retrieval_cache.db")

    # 命中缓存时跳过目录树检索，只读取已定位的条文页。
    cached_documents = cache.get(question) if doc_ids is None else []
    if cached_documents:
        documents = {
            document["name"]: document["id"]
            for document in client.list_documents()["documents"]
        }
    else:
        documents = {}
    if cached_documents and all(document.doc_name in documents for document in cached_documents):
        print("-----------------触发缓存-------------------------")
        markdown_parts: list[str] = []
        for cached_document in cached_documents:
            # 每部法规的一组条文页通过一次 PageIndex 调用读回。
            pages = client.get_page_content(
                documents[cached_document.doc_name], cached_document.pages
            )
            
            clause_text = "\n\n".join(page["markdown"] for page in pages)
            markdown_parts.append(f"法规：{cached_document.doc_name}\n{clause_text}")
        return "\n\n".join(markdown_parts)

    # 未命中缓存时，调用 PageIndex 完整执行文档树和条文页检索。
    response = client.responses(question, doc_id=doc_ids, max_turns=7)

    # 输出本轮模型实际调用过的检索工具，便于查看检索路径。
    print("检索路径：")
    for item in response["items"]:
        if item.get("type") == "function_call":
            print(f"- {item['name']}({item['arguments']})")

    # 全库检索结果才写入缓存，避免限定法规范围污染同问题的缓存。
    if doc_ids is None:
        cache_retrieval_paths(cache, question, response)

    final_message = response["output"][-1]
    answer = final_message["content"][0]["text"]
    return answer


def cache_retrieval_paths(
    cache: RetrievalCache,
    question: str,
    response: dict,
) -> None:
    """缓存本轮各法规的 get_page_content 多页参数。"""

    # 只缓存实际读取过的条文页，不缓存目录浏览或最终回答。
    page_calls = [
        item for item in response["items"]
        if item.get("type") == "function_call" and item.get("name") == "get_page_content"
    ]

    # 按法规名合并多次工具调用中的原始页码参数。
    document_pages: dict[str, list[str]] = {}
    for page_call in page_calls:
        arguments = json.loads(page_call["arguments"])
        document_name = arguments.get("doc_name")
        pages = arguments.get("pages")
        if isinstance(document_name, str) and isinstance(pages, str) and pages:
            document_pages.setdefault(document_name, []).append(pages)

    cached_documents = [
        CachedDocument(
            doc_name=document_name,
            pages=",".join(pages),
        )
        for document_name, pages in document_pages.items()
    ]
    if cached_documents:
        # 同一问题的旧定位整体替换，避免保留过期或错误的页。
        cache.put(question, cached_documents)


def main() -> None:
    parser = argparse.ArgumentParser(description="使用 PageIndex 索引提问")
    parser.add_argument("question",nargs="?", default=DEFAULT_QUESTION, help=f"要询问的问题")
    parser.add_argument("--doc-ids", nargs="+", default=None, help="限定一部或多部法规的 doc_id；不传时检索整个本地法规库")
    parser.add_argument("--storage-path", type=Path, default=Path(".pageindex"), help="索引缓存目录，默认：.pageindex")
    args = parser.parse_args()

    answer = ask_question(args.doc_ids, args.question, args.storage_path)
    print(answer)


if __name__ == "__main__":
    main()
