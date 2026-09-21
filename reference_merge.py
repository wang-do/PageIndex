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
    """合并同法规、同 PDF 页、条款号连续的引用（范围形态，如 6.8.1~6.8.5）。

    分组改为按键聚合（顺序无关）：双源提取后同页条文可能被其他法规的
    引用隔开，必须按键归组而不是按"相邻"归组，否则同页条文会被拆开。"""
    if len(references) < 2:
        return references

    groups: dict[tuple, list[dict]] = {}
    group_order: list[tuple] = []
    for ref in references:
        key = (ref.get("spec_no"), ref.get("page_pdf"))
        if key not in groups:
            groups[key] = []
            group_order.append(key)
        groups[key].append(ref)

    merged: list[dict] = []
    for key in group_order:
        items = groups[key]
        ordered = sorted(items, key=lambda r: _clause_sort_key(r["clause_no"]))
        # 组内按"前缀相同且尾号连续"切连续段。
        segments: list[dict] = []
        for item in ordered:
            prefix, tail = _clause_sort_key(item["clause_no"])
            prev = segments[-1] if segments else None
            if (prev and prev["prefix"] == prefix and prev["tail"] is not None
                    and tail == prev["tail"] + 1):
                prev["items"].append(item)
                prev["tail"] = tail
            else:
                segments.append({"prefix": prefix, "tail": tail, "items": [item]})

        for segment in segments:
            seg_items = segment["items"]
            first, last = seg_items[0], seg_items[-1]
            boxes = [it["bbox"] for it in seg_items if it.get("bbox")]
            bbox = (
                [min(b[0] for b in boxes), min(b[1] for b in boxes),
                 max(b[2] for b in boxes), max(b[3] for b in boxes)]
                if boxes and all(b for b in boxes) else first.get("bbox")
            )
            clause_no = (first["clause_no"] if first is last
                         else f"{first['clause_no']}~{last['clause_no']}")
            source = "page" if any(it.get("source") == "page" for it in seg_items) else "leaf"
            merged.append({**first, "clause_no": clause_no, "bbox": bbox, "source": source})
    return merged

    # 按键聚合（顺序无关）：双源提取后同一页的条文可能被其他法规的
    # 引用隔开，必须按键归组而不是按"相邻"归组，否则同页条文会被拆开。
    groups: dict[tuple, list[dict]] = {}
    group_order: list[tuple] = []
    for ref in references:
        key = (ref.get("spec_no"), ref.get("page_pdf"))
        if key not in groups:
            groups[key] = []
            group_order.append(key)
        groups[key].append(ref)

    merged: list[dict] = []
    for key in group_order:
        items = groups[key]
        ordered = sorted(items, key=lambda r: _clause_sort_key(r["clause_no"]))
        # 组内按"前缀相同且尾号连续"切连续段。
        segments: list[dict] = []
        for item in ordered:
            prefix, tail = _clause_sort_key(item["clause_no"])
            prev = segments[-1] if segments else None
            if (prev and prev["prefix"] == prefix and prev["tail"] is not None
                    and tail == prev["tail"] + 1):
                prev["items"].append(item)
                prev["tail"] = tail
            else:
                segments.append({"prefix": prefix, "tail": tail, "items": [item]})

        for segment in segments:
            seg_items = segment["items"]
            first, last = seg_items[0], seg_items[-1]
            boxes = [it["bbox"] for it in seg_items if it.get("bbox")]
            bbox = (
                [min(b[0] for b in boxes), min(b[1] for b in boxes),
                 max(b[2] for b in boxes), max(b[3] for b in boxes)]
                if boxes and all(b for b in boxes) else first.get("bbox")
            )
            clause_no = (first["clause_no"] if first is last
                         else f"{first['clause_no']}~{last['clause_no']}")
            merged.append({**first, "clause_no": clause_no, "bbox": bbox})
    return merged
