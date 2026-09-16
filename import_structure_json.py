"""将已有 PageIndex 结构 JSON 导入本地 PageIndex 文档库。"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from pageindex import PageIndexLocalClient


PROJECT_DIR = Path(__file__).resolve().parent

## 可以把需要读的法规全部放在这
DEFAULT_JSON_PATHS = "C:\\Users\\localuser\\Desktop\\王栋焱\\法律RAG\\json后处理\\pageindex_json"

# 原始 tree JSON（含 position：PDF 页码/印刷页码/页内 bbox）所在目录。
# 导入时归档为 .pageindex/docs/{doc_id}/raw_tree.json，供引用定位使用。
TREE_JSON_DIR = PROJECT_DIR.parent / "json后处理" / "tree_json"



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


def archive_raw_tree(json_path: Path, doc_id: str, storage_path: Path) -> bool:
    """把带 position 的原始 tree JSON 归档到文档目录（raw_tree.json）。"""
    tree_src = TREE_JSON_DIR / f"{json_path.stem.removesuffix('_pageindex')}_tree.json"
    if not tree_src.exists():
        return False
    dest = storage_path / "docs" / doc_id / "raw_tree.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(tree_src, dest)
    return True


def import_structure_json(json_paths: Path, storage_path: Path) -> list[dict]:
    """导入多份法规 JSON；同名文档已存在时跳过。"""

    if json_paths == "":
        raise NotADirectoryError(f"JSON 目录不存在：{json_paths}")

    client = create_client(storage_path)
    existing_names = {
        document["name"] for document in client.list_documents()["documents"]
    }
    results: list[dict] = []

    json_list = list(Path(json_paths).glob('*.json'))

    for json_path in json_list:

        document_name = get_document_name(json_path)
        if document_name in existing_names:
            results.append({"name": document_name, "status": "skipped"})
            continue
        result = client.submit_structure_json(str(json_path))
        doc_id = result["doc_id"]
        archived = archive_raw_tree(json_path, doc_id, storage_path)
        results.append({"name": result["name"], "doc_id": doc_id,
                        "status": "imported", "raw_tree": archived})
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
