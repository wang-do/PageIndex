"""将已有 PageIndex 结构 JSON 导入本地 PageIndex 文档库。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from pageindex import PageIndexLocalClient


PROJECT_DIR = Path(__file__).resolve().parent

## 可以把需要读的法规全部放在这
DEFAULT_JSON_PATHS = [
    PROJECT_DIR / "demo_regulations" / "GB55025-2022.json",
]


def create_client(storage_path: Path) -> PageIndexLocalClient:
    """创建用于导入法规 JSON 的本地 PageIndex 客户端。"""
    return PageIndexLocalClient(
        index_model="deepseek/deepseek-chat",
        chat_model="deepseek-chat",
        storage_path=str(storage_path),
    )


def get_document_name(json_path: Path) -> str:
    """读取 JSON 的文档标识，用于避免重复导入。"""
    with json_path.open("r", encoding="utf-8") as file:
        document_name = json.load(file).get("doc_name")
    if not isinstance(document_name, str) or not document_name.strip():
        raise ValueError(f"JSON 缺少非空 doc_name：{json_path}")
    return document_name


def import_structure_json(json_paths: list[Path], storage_path: Path) -> list[dict]:
    """导入多份法规 JSON；同名文档已存在时跳过。"""
    client = create_client(storage_path)
    existing_names = {
        document["name"] for document in client.list_documents()["documents"]
    }
    results: list[dict] = []
    for json_path in json_paths:
        if not json_path.exists():
            raise FileNotFoundError(f"JSON 文件不存在：{json_path}")
        document_name = get_document_name(json_path)
        if document_name in existing_names:
            results.append({"name": document_name, "status": "skipped"})
            continue
        result = client.submit_structure_json(str(json_path))
        results.append({"name": result["name"], "doc_id": result["doc_id"], "status": "imported"})
        existing_names.add(result["name"])
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="导入 PageIndex 结构 JSON")
    parser.add_argument(
        "json_paths",
        nargs="*",
        type=Path,
        default=DEFAULT_JSON_PATHS,
        help="一份或多份包含 structure 和 pages 的 JSON；不传时导入两份演示法规",
    )
    parser.add_argument(
        "--storage-path",
        type=Path,
        default=Path(".pageindex"),
        help="PageIndex 本地存储目录，默认：.pageindex",
    )
    args = parser.parse_args()

    results = import_structure_json(args.json_paths, args.storage_path)
    for result in results:
        if result["status"] == "imported":
            print(f"导入完成：{result['name']}，doc_id={result['doc_id']}")
        else:
            print(f"跳过已导入文档：{result['name']}",)


if __name__ == "__main__":
    main()
