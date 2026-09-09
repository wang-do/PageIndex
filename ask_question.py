"""使用已有的 PageIndex 索引提问。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Optional

from pageindex import PageIndexLocalClient
from retrieval_cache import CachedPage, RetrievalCache


DEFAULT_QUESTION = "宿舍、旅馆选址有什么规定？"


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
    cached_pages = cache.get(question) if doc_ids is None else []
    if cached_pages:
        print("检索路径：")
        markdown_parts: list[str] = []
        for cached_page in cached_pages:
            # 一个问题可能命中多部法规或多个条文页，逐页取回原文。
            page = client.get_page_content(
                cached_page.doc_id, str(cached_page.page_index)
            )[0]
            print(
                f"- retrieval_cache(doc_id={cached_page.doc_id}, "
                f"page_index={cached_page.page_index})"
            )
            markdown_parts.append(page["markdown"])
        return "\n\n".join(markdown_parts)

    # 未命中缓存时，调用 PageIndex 完整执行文档树和条文页检索。
    response = client.responses(question, doc_id=doc_ids, max_turns=5)

    # 输出本轮模型实际调用过的检索工具，便于查看检索路径。
    print("检索路径：")
    for item in response["items"]:
        if item.get("type") == "function_call":
            print(f"- {item['name']}({item['arguments']})")

    # 全库检索结果才写入缓存，避免限定法规范围污染同问题的缓存。
    if doc_ids is None:
        cache_retrieval_paths(cache, client, question, response)

    final_message = response["output"][-1]
    answer = final_message["content"][0]["text"]
    return answer


def cache_retrieval_paths(
    cache: RetrievalCache,
    client: PageIndexLocalClient,
    question: str,
    response: dict,
) -> None:
    """缓存本轮所有 get_page_content 调用定位到的逻辑页。"""

    # 只缓存实际读取过的条文页，不缓存目录浏览或最终回答。
    page_calls = [
        item for item in response["items"]
        if item.get("type") == "function_call" and item.get("name") == "get_page_content"
    ]

    # 工具参数使用文档名，这里转换为本地存储使用的 doc_id。
    documents = {
        document["name"]: document["id"]
        for document in client.list_documents()["documents"]
    }
    cached_pages: list[CachedPage] = []
    for page_call in page_calls:
        arguments = json.loads(page_call["arguments"])
        document_id = documents.get(arguments.get("doc_name"))
        page_indexes = expand_page_indexes(arguments.get("pages"))
        if document_id is not None:
            cached_pages.extend(
                CachedPage(doc_id=document_id, page_index=page_index)
                for page_index in page_indexes
            )
    if cached_pages:
        # 同一问题的旧定位整体替换，避免保留过期或错误的页。
        cache.put(question, cached_pages)


def expand_page_indexes(pages: object) -> list[int]:
    """展开 PageIndex 的单页、逗号页和连续页范围参数。"""

    # PageIndex 可能返回 "5"、"2,5" 或 "5-7"，统一展开为页码列表。
    if not isinstance(pages, str):
        return []
    indexes: list[int] = []
    for part in pages.split(","):
        if part.isdigit():
            indexes.append(int(part))
            continue
        start_end = part.split("-", maxsplit=1)
        if len(start_end) != 2 or not all(value.isdigit() for value in start_end):
            return []
        start, end = (int(value) for value in start_end)
        if start > end:
            return []
        indexes.extend(range(start, end + 1))
    return indexes


def main() -> None:
    parser = argparse.ArgumentParser(description="使用 PageIndex 索引提问")
    parser.add_argument(
        "question",
        nargs="?",
        default=DEFAULT_QUESTION,
        help=f"要询问的问题，默认：{DEFAULT_QUESTION}",
    )
    parser.add_argument(
        "--doc-ids",
        nargs="+",
        default=None,
        help="限定一部或多部法规的 doc_id；不传时检索整个本地法规库",
    )
    parser.add_argument(
        "--storage-path",
        type=Path,
        default=Path(".pageindex"),
        help="索引缓存目录，默认：.pageindex",
    )
    args = parser.parse_args()

    answer = ask_question(args.doc_ids, args.question, args.storage_path)
    print(answer)


if __name__ == "__main__":
    main()
