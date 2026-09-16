"""按法规名 + 逻辑页号反查条文的 PDF 定位数据（画红框必需字段）。

数据源（均在 .pageindex/docs/{doc_id}/ 内，自包含）：
- pages.json    逻辑页数组：markdown 以条款号开头（"3.1.2 宿舍……"）
- raw_tree.json 原始树：clause_no → position(page_pdf/bbox)

注意：不使用 tree.json 建映射——部分早期导入的文档存储树不完整
（条文叶大量缺失），而 pages.json 的条款号前缀是转换时必然写入的。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

STORAGE_PATH = Path(".pageindex")

_CLAUSE_PREFIX = re.compile(r"^([0-9A-Za-z][0-9A-Za-z.]*)\s")

# doc_name -> {"spec_no": str, "spec_name": str, "pages": {逻辑页号: 定位条目}}
_docs_cache: dict[str, dict] = {}


def _walk_raw(node, by_node: dict) -> None:
    """收集原始树条文节点：clause_no → 画红框必需的定位数据。"""
    if not isinstance(node, dict):
        return
    clause_no = node.get("clause_no")
    if clause_no:
        position = node.get("position") or {}
        by_node[clause_no] = {
            "clause_no": clause_no,
            "page_pdf": position.get("page_pdf"),
            "bbox": position.get("bbox"),
            "img_w": position.get("img_w"),
            "img_h": position.get("img_h"),
        }
    for child in node.get("child_nodes") or []:
        _walk_raw(child, by_node)


def _load_all() -> None:
    """一次性加载全部文档的定位索引（模块级缓存）。"""
    if _docs_cache:
        return
    docs_dir = STORAGE_PATH / "docs"
    for doc_dir in sorted(docs_dir.iterdir()):
        doc_json = doc_dir / "doc.json"
        raw_json = doc_dir / "raw_tree.json"
        pages_json = doc_dir / "pages.json"
        if not (doc_json.exists() and raw_json.exists() and pages_json.exists()):
            continue
        doc = json.loads(doc_json.read_text(encoding="utf-8"))
        raw = json.loads(raw_json.read_text(encoding="utf-8"))
        by_node: dict = {}
        _walk_raw(raw, by_node)

        pages: dict = {}
        pages_data = json.loads(pages_json.read_text(encoding="utf-8"))
        for page in pages_data if isinstance(pages_data, list) else []:
            page_index = page.get("page_index")
            markdown = page.get("markdown") or ""
            match = _CLAUSE_PREFIX.match(markdown.strip())
            if not match or match.group(1) not in by_node:
                continue  # 目录页/无条款号前缀的页：引用时跳过
            pages[page_index] = by_node[match.group(1)]
        if not pages:
            continue
        metadata = raw.get("metadata") or {}
        _docs_cache[doc.get("name", "")] = {
            # 去空格规范号，即 PDF 文件名基（GB 50016-2014 → GB50016-2014.pdf）
            "spec_no": str(metadata.get("spec_no", "")).replace(" ", ""),
            "spec_name": metadata.get("spec_name") or doc.get("name", ""),
            "pages": pages,
        }


def _expand_pages(pages: str) -> list[int]:
    """展开 '16-23' / '7-8,26-27' / '322,330' 为逻辑页号列表。"""
    result: list[int] = []
    for part in pages.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start, end = part.split("-", 1)
            result.extend(range(int(start), int(end) + 1))
        else:
            result.append(int(part))
    return result


def resolve(doc_name: str, pages: str) -> list[dict]:
    """法规名 + 逻辑页号 → 画红框必需字段列表。"""
    _load_all()
    doc = _docs_cache.get(doc_name)
    if not doc:
        return []
    result = []
    for page_index in _expand_pages(pages):
        entry = doc["pages"].get(page_index)
        if not entry:
            continue
        result.append({
            "spec_no": doc["spec_no"],
            "spec_name": doc["spec_name"],
            "clause_no": entry["clause_no"],
            "page_pdf": entry["page_pdf"],
            "bbox": entry["bbox"],
            "img_w": entry["img_w"],
            "img_h": entry["img_h"],
        })
    return result
