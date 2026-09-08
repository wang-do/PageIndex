"""将已有 PageIndex 结构 JSON 导入本地 PageIndex 文档库。"""

from __future__ import annotations

import argparse
from pathlib import Path

from pageindex import PageIndexLocalClient


def import_structure_json(json_path: Path, storage_path: Path) -> str:
    """导入结构树和页面原文，返回可供 client.chat 使用的 doc_id。"""
    client = PageIndexLocalClient(
        index_model="deepseek/deepseek-chat",
        chat_model="deepseek/deepseek-chat",
        storage_path=str(storage_path),
    )
    result = client.submit_structure_json(str(json_path))
    return result["doc_id"]


def main() -> None:
    parser = argparse.ArgumentParser(description="导入 PageIndex 结构 JSON")
    parser.add_argument("json_path", type=Path, help="包含 structure 和 pages 的 JSON 文件", default="C:\\Users\\localuser\\Desktop\\王栋焱\\法律RAG\\PageIndex-main\\demo_building_code_pageindex.json")
    parser.add_argument(
        "--storage-path",
        type=Path,
        default=Path(".pageindex"),
        help="PageIndex 本地存储目录，默认：.pageindex",
    )
    args = parser.parse_args()

    doc_id = import_structure_json(args.json_path, args.storage_path)
    print(f"导入完成，doc_id={doc_id}")
    print("可用 ask_question.py 对该 doc_id 提问。")


if __name__ == "__main__":
    main()
