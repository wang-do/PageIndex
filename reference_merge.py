"""引用列表合并：同法规、同 PDF 页且条款号连续的引用合并为一条。

合并规则：bbox 取组内并集（红框圈住整段连续条文），条款号显示为
范围（'6.4.1~6.4.2'）；跨页或编号不连续的不合并。
"""

from __future__ import annotations

import re

_CLAUSE_TAIL = re.compile(r"^(.*?)(\d+)$")


def _clause_sort_key(clause_no: str) -> tuple:
    """条款号排序键：前缀 + 数字尾号（'6.4.2' → ('6.4.', 2)）。"""
    match = _CLAUSE_TAIL.match(clause_no)
    return (match.group(1), int(match.group(2))) if match else (clause_no, -1)


def merge_references(references: list[dict]) -> list[dict]:
    """引用去重（单条形态）——JSON 模式下 c 引用精确到条，每条自带 bbox。

    去重键：(法规, 条款号, 页号)。顺序保持首次出现。
    单条 bbox 来自 raw_tree 的精确 position，PDF 跳转红框按条定位。
    """
    seen: set = set()
    unique: list[dict] = []
    for ref in references:
        key = (ref.get("spec_no"), ref.get("clause_no"), ref.get("page_pdf"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(ref)
    return unique


