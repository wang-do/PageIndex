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
import re
from pathlib import Path
from time import perf_counter
from typing import Optional

from conversation_history import ConversationHistory
from clause_index import resolve as resolve_refs
from pageindex import PageIndexLocalClient
from reference_merge import merge_references
from retrieval_cache import RetrievalCache, is_complete_question


DEFAULT_QUESTION = "宿舍、旅馆选址有什么规定？GB55025-2022"
# DEFAULT_QUESTION = "详细说一下第一条"

_INSTRUCTION_ZH = (
    "始终使用简体中文回答——包括工具调用之间的任何过程说明，一律用简体中文。"

    "导航方法：对长文档先用 get_document_structure(doc_name) 查看第一层"
    "章节列表；选中相关章节后，把该节点的 node_id 传回"
    " get_document_structure(doc_name, node_id=…) 逐层下钻——响应里"
    " has_children=true 的节点可继续下钻，has_children=false 的条文叶带"
    " start_index 页号；定位到目标条文页后用 get_page_content(doc_name,"
    " pages=页号) 读取条文。每层只返回该层节点，不要期望一次看到全文档目录。"
    "多法规召回（硬性要求）：同一规定往往同时出现在多本规范中——强制性"
    "通用规范与专门标准"
    "条文大量同文。browse 之后，必须从法规列表中找出【所有】与问题主题可能"
    "相关的法规并逐一检索，宁多勿漏：不能因为在一本规范里找到了答案就停止，"
    "凡是名称、描述或章节概要与问题主题沾边的规范都要下钻核对。回答时把"
    "检索到的多本规范的相关条文并列引用，标明各自出处。"
    "回答要求：对检索到的法规内容做总结提炼后回答，禁止原样照抄条文原文"
    "或逐条罗列条文。回答的粒度要与提问匹配：宽泛的问题（如「××有什么"
    "要求」）就给出宽泛的概括——点出涉及哪些规范、哪些章节、核心要求是什么"
    "即可，不需要细化到每一条；只有提问明确针对某一条的具体内容时才展开"
    "该条。尽可能精简，能一句话说清的不写两句。提到具体条文时标注出处"
    "（如「GB50016 第6.4.1条」）。"
    "硬性规则：凡是与法规、条文、技术要求相关的问题，无论对话历史中是否"
    "已有相关内容，都必须先调用检索工具查找当前适用的条文，再基于检索结果"
    "组织回答；而且必须调用 get_page_content 获取条文原文——仅浏览文档目录"
    "或章节结构不算完成检索，禁止在未读取条文原文的情况下回答法规类问题。"
    "特别注意：即使你自认为知道答案（消防车道、防火门等常见设施的规定你"
    "记忆中有），也必须先检索——你的记忆可能过时、不完整，且所有回答都必须"
    "能通过引用溯源。严禁仅凭对话历史中的条文摘录回答法规类问题——历史摘录"
    "只是过往查询留下的片段，往往不完整，也可能与当前问题的适用范围不符。"
    "唯一例外：与法规检索完全无关的纯对话（打招呼、让你复述上一句话等）"
    "可以直接回应。"
)






def _references_from_items(items: list[dict]) -> list[dict]:
    """从 agentic transcript 提取全部条文溯源。

    两个来源（去重后合并）：
    1. get_page_content 的 (doc_name, pages) —— 读了条文原文的页
    2. get_document_structure 响应中的条文叶（has_children=false、summary
       以条款号开头）—— 模型在逐层下钻时直接"读到"的条文（summary=全文），
       即使没有再调 get_page_content 也要计入溯源
    """
    outputs = {it.get("call_id"): it.get("output", "")
               for it in items
               if isinstance(it, dict) and it.get("type") == "function_call_output"}
    doc_pages: list[tuple[str, str]] = []
    structure_leaves: list[tuple[str, int]] = []
    for it in items:
        if not (isinstance(it, dict) and it.get("type") == "function_call"):
            continue
        name = it.get("name")
        try:
            args = json.loads(it.get("arguments") or "{}")
        except (TypeError, ValueError):
            continue
        if name == "get_page_content":
            if args.get("doc_name") and args.get("pages"):
                doc_pages.append((args["doc_name"], args["pages"]))
        elif name == "get_document_structure":
            if not args.get("doc_name"):
                continue
            out = outputs.get(it.get("call_id"), "")
            try:
                env = json.loads(out) if isinstance(out, str) else {}
            except (TypeError, ValueError):
                continue
            doc_name = args.get("doc_name")
            for child in env.get("children") or []:
                if not isinstance(child, dict) or child.get("has_children"):
                    continue  # 只取条文叶
                page = child.get("start_index")
                if not isinstance(page, int):
                    continue
                structure_leaves.append((doc_name, page))

    references = [
        {**ref, "source": "page"}          # 读了原文的页 → page 来源
        for doc_name, pages in doc_pages
        for ref in resolve_refs(doc_name, pages)
    ]
    seen = {(r.get("spec_no"), r.get("clause_no")) for r in references}
    for doc_name, page in structure_leaves:
        for r in resolve_refs(doc_name, str(page)):
            key = (r.get("spec_no"), r.get("clause_no"))
            if key not in seen:
                seen.add(key)
                references.append({**r, "source": "leaf"})   # 下钻路过的叶子 → leaf 来源
    return merge_references(references)


_CITE_PAIR = re.compile(
    r"([A-Z]{2,}[0-9A-Za-z/]*\s*[-—–]?\s*[0-9]{4}(?:\([0-9]{4}\))?)\s*"
    r"(?:第|条款?)?\s*([0-9]+(?:\.[0-9]+)+)"
)


def _filter_cited(answer: str, references: list[dict]) -> list[dict]:
    """确定性引用对账：只保留回答文本中出现的条款号对应的条文。

    回答里写了 (规范号) 条款号 → 与实读条文匹配（规范号包含 +
    条款号精确/范围覆盖，"6.4.1~6.4.5" 覆盖 6.4.3）。
    匹配失败时保留全部（不产生空引用）。"""
    if not references or not answer:
        return references
    flat = re.sub(r"[\s（）()]", "", answer)
    pairs = [(m.group(1), m.group(2)) for m in _CITE_PAIR.finditer(flat)]
    if not pairs:
        return references

    def _ckey(clause: str) -> tuple:
        return tuple(int(x) if x.isdigit() else 0 for x in clause.split("."))

    def _covers(rclause: str, pclause: str) -> bool:
        if rclause == pclause or pclause in rclause:
            return True
        if "~" in rclause:
            a, _, b = rclause.partition("~")
            try:
                return _ckey(a) <= _ckey(pclause) <= _ckey(b)
            except Exception:
                return False
        return False

    picked: list[dict] = []
    for r in references:
        spec = re.sub(r"[\s（）()]", "", r.get("spec_no") or "")
        rclause = r.get("clause_no") or ""
        for pspec, pclause in pairs:
            if pspec and (pspec in spec or spec in pspec) and _covers(rclause, pclause):
                picked.append(r)
                break
    return picked if picked else references


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
    
    # 多轮防幻觉：历史 assistant 回答截断为开头摘要。完整条文/总结留在
    # 会话历史与引用按钮里供用户查看，但不再进入模型上下文——消除
    # "历史里有资料可不检索直接作答"的诱因，新问题必须重新检索。
    messages = [
        {**m, "content": (
            m["content"][:120] + "……（历史回答已归档；回答新问题请重新检索条文）"
            if m["role"] == "assistant" and len(m["content"]) > 120
            else m["content"])}
        for m in messages
    ]
    messages.append({"role": "user", "content": question})

    complete_question = is_complete_question(question)
    # 只有能独立理解的问题才查全局缓存，避免追问脱离上下文误命中。
    # 两级精确键：① 全文键（一模一样的问题）② jieba 关键词串键（句式变体）。
    cached_entry = None
    if doc_ids is None and complete_question:
        cached_entry = cache.get(question) or cache.get_by_keyword(question)

    # 命中：要点总结 + 引用成品直接回放（零模型调用、零重建）。
    if cached_entry and cached_entry[0]:
        answer, references = cached_entry[0], cached_entry[1] or []
        elapsed = round(perf_counter() - started_at, 1)
        history.append_turn(session_id, question, answer, cached=True, tokens=None,
                            elapsed=elapsed, references=references)
        yield {"type": "delta", "text": answer}
        yield {"type": "done", "answer": answer, "cached": True, "tokens": None,
               "elapsed": elapsed, "references": references}
        return

    # 未命中：流式 agentic 导航。
    try:
        events = client.responses(
            messages,
            doc_id=doc_ids,
            max_turns=8,  # 硬上限：封顶发散（提示词是软约束，轮数是硬保证）
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
    # 结构化引用：从 transcript 提取（page_content 页 + structure 条文叶），去重合并。
    # 保留全部实读条文：段落标注负责命中筛选，底部列表兜底展示，依据不丢失。
    references = _references_from_items(items)
    history.append_turn(session_id, question, answer,
                        cached=False, tokens=tokens or None, elapsed=elapsed,
                        references=references)

    # 只有完整问题的全库检索结果才写入全局缓存（要点总结 + 引用成品）。
    if doc_ids is None and complete_question:
        cache.put(question, summary=answer,
                  references_json=json.dumps(references, ensure_ascii=False))

    yield {"type": "done", "answer": answer, "cached": False,
           "tokens": tokens or None, "elapsed": elapsed, "references": references}


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
