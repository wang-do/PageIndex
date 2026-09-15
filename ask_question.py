"""使用已有的 PageIndex 索引提问（SSE 事件流版本）。

ask_question() 是生成器，逐步产出事件 dict：
  {"type": "step",  "text": "查阅《GB50016》3.6.4 页"}   检索过程
  {"type": "delta", "text": "..."}                        答案文本增量
  {"type": "done",  "answer": ..., "cached": ..., "tokens": ..., "elapsed": ...}
  {"type": "error", "detail": "..."}
导航结束时照旧写入检索缓存与会话历史（含 cached/tokens/elapsed 统计）。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from time import perf_counter
from typing import Optional

from conversation_history import ConversationHistory
from pageindex import PageIndexLocalClient
from retrieval_cache import CachedDocument, RetrievalCache, is_complete_question


DEFAULT_QUESTION = "宿舍、旅馆选址有什么规定？GB55025-2022"
# DEFAULT_QUESTION = "详细说一下第一条"

_INSTRUCTION_ZH = (
    "始终使用简体中文回答。"
    "硬性规则：凡是与法规、条文、技术要求相关的问题，无论对话历史中是否"
    "已有相关内容，都必须先调用检索工具查找当前适用的条文，再基于检索结果"
    "组织回答。严禁仅凭对话历史中的条文摘录回答法规类问题——历史摘录只是"
    "过往查询留下的片段，往往不完整，也可能与当前问题的适用范围不符。"
    "唯一例外：与法规检索完全无关的纯对话（打招呼、让你复述上一句话等）"
    "可以直接回应。"
)


def _describe_tool(item: dict) -> str:
    """把一次工具调用翻译成一句人话，用于前端检索过程展示。"""
    name = item.get("name") or ""
    try:
        args = json.loads(item.get("arguments") or "{}")
    except (TypeError, ValueError):
        args = {}
    doc = args.get("doc_name") or ""
    pages = args.get("pages") or ""
    if name == "browse_documents":
        return "浏览法规库"
    if name == "get_document_structure":
        return f"查看《{doc}》目录" if doc else "查看文档目录"
    if name == "get_page_content":
        return f"查阅《{doc}》第{pages} 条法规" if pages else f"查阅《{doc}》"
    return name or "调用工具"


def ask_question(
    doc_ids: Optional[list[str]],
    question: str,
    storage_path: Path,
    session_id: str = "default",
    history_turns: int = 5,
):
    """带最近对话上下文提问，逐步产出事件；结束时写缓存与历史。"""
    started_at = perf_counter()
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
    # 两级精确键：① 全文键（一模一样的问题）② jieba 关键词串键（句式变体）。
    if doc_ids is None and complete_question:
        cached_documents = cache.get(question) or cache.get_by_keyword(question)
    else:
        cached_documents = []
    if cached_documents:
        documents = {
            document["name"]: document["id"]
            for document in client.list_documents()["documents"]
        }
    else:
        documents = {}

    if cached_documents and all(document.doc_name in documents for document in cached_documents):
        yield {"type": "step", "text": "命中缓存，直接读取条文"}
        markdown_parts: list[str] = []
        for cached_document in cached_documents:
            # 每部法规的一组条文页通过一次 PageIndex 调用读回。
            pages = client.get_page_content(
                documents[cached_document.doc_name], cached_document.pages
            )

            clause_text = "\n\n".join(page["markdown"] for page in pages)
            markdown_parts.append(f"法规：{cached_document.doc_name}\n{clause_text}")
        answer = "\n\n".join(markdown_parts)
        elapsed = round(perf_counter() - started_at, 1)
        history.append_turn(session_id, question, answer, cached=True, tokens=None, elapsed=elapsed)
        yield {"type": "delta", "text": answer}
        yield {"type": "done", "answer": answer, "cached": True, "tokens": None, "elapsed": elapsed}
        return

    # 未命中：流式 agentic 导航，把工具调用与答案增量实时抛给调用方。
    try:
        events = client.responses(
            messages,
            doc_id=doc_ids,
            max_turns=7,
            # 追加到系统提示末尾：默认提示词全英文，模型会跟着说英文。
            instructions=_INSTRUCTION_ZH,
            stream=True,
        )
        final: Optional[dict] = None
        for event in events:
            etype = event.get("type")
            if etype == "response.output_item.done":
                item = event.get("item") or {}
                if item.get("type") == "function_call":
                    yield {"type": "step", "text": _describe_tool(item)}
            elif etype == "response.output_text.delta":
                delta = event.get("delta") or ""
                if delta:
                    yield {"type": "delta", "text": delta}
            elif etype in ("response.completed", "response.incomplete",
                           "response.failed"):
                final = event.get("response") or {}
        if final is None:
            raise RuntimeError("模型未返回结果")
    except Exception as exc:
        yield {"type": "error", "detail": str(exc)}
        return

    # 流结束：从最终 envelope 取权威数据写缓存与历史。
    output = final.get("output") or []
    items = final.get("items") or []
    # 三个输出量 是否命中缓存 token使用量 使用时长
    usage = final.get("usage") or {}
    tokens = (usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0)
    elapsed = round(perf_counter() - started_at, 1)

    answer = ""
    if output:
        last = output[-1]
        answer = "".join(
            part.get("text", "") for part in (last.get("content") or [])
            if isinstance(part, dict)
        )
    history.append_turn(session_id, question, answer,
                        cached=False, tokens=tokens or None, elapsed=elapsed)

    # 只有完整问题的全库检索结果才写入全局缓存。
    if doc_ids is None and complete_question:
        cache_retrieval_paths(cache, question, {"items": items})

    yield {"type": "done", "answer": answer, "cached": False,
           "tokens": tokens or None, "elapsed": elapsed}


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

    for event in ask_question(
        args.doc_ids,
        args.question,
        args.storage_path,
        args.session_id,
        args.history_turns,
    ):
        if event["type"] == "step":
            print(f"[检索] {event['text']}")
        elif event["type"] == "delta":
            print(event["text"], end="", flush=True)
        elif event["type"] == "done":
            print()
        elif event["type"] == "error":
            print(f"[错误] {event['detail']}")


if __name__ == "__main__":
    main()
