"""使用已有的 PageIndex 索引提问。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Optional

from conversation_history import ConversationHistory
from pageindex import PageIndexLocalClient
from retrieval_cache import CachedDocument, RetrievalCache, is_complete_question


DEFAULT_QUESTION = "宿舍、旅馆选址有什么规定？GB55025-2022"
# DEFAULT_QUESTION = "详细说一下第一条"

def ask_question(
    doc_ids: Optional[list[str]],
    question: str,
    storage_path: Path,
    session_id: str = "default",
    history_turns: int = 3,
) -> str:
    """带最近对话上下文提问，并优先复用单轮精确缓存。"""
    client = PageIndexLocalClient(
        index_model="deepseek/deepseek-chat",
        chat_model="deepseek-chat",
        storage_path=str(storage_path),
    )
    cache = RetrievalCache(storage_path / "retrieval_cache.db")
    history = ConversationHistory(storage_path / "conversation_history.db")
    messages = history.get_recent(session_id, history_turns)
    messages.append({"role": "user", "content": question})

    complete_question = is_complete_question(question)
    # 只有能独立理解的问题才查全局缓存，避免追问脱离上下文误命中。
    cached_documents = (
        cache.get(question) if doc_ids is None and complete_question else []
    )
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
        answer = "\n\n".join(markdown_parts)
        history.append_turn(session_id, question, answer)
        return answer

    # PageIndex 原生支持 role/content 消息列表，并按完整上下文检索。
    response = client.responses(messages, doc_id=doc_ids, max_turns=7)

    # 输出本轮模型实际调用过的检索工具，便于查看检索路径。
    print("检索路径：")
    for item in response["items"]:
        if item.get("type") == "function_call":
            print(f"- {item['name']}({item['arguments']})")

    # 只有完整问题的全库检索结果才写入全局缓存。
    if doc_ids is None and complete_question:
        cache_retrieval_paths(cache, question, response)

    final_message = response["output"][-1]
    answer = final_message["content"][0]["text"]
    history.append_turn(session_id, question, answer)
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
    parser.add_argument("--session-id", default="default", help="对话会话 ID，默认：default")
    parser.add_argument("--history-turns", type=int, default=3, help="携带的最近对话轮数，默认：3")
    args = parser.parse_args()

    answer = ask_question(
        args.doc_ids,
        args.question,
        args.storage_path,
        args.session_id,
        args.history_turns,
    )
    print(answer)


if __name__ == "__main__":
    main()
