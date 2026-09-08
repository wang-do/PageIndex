"""使用 PageIndex 为 PDF 建立本地层级索引。"""

from __future__ import annotations

import argparse
from pathlib import Path

from pageindex import PageIndexClient


def build_index(pdf_path: Path, storage_path: Path) -> str:
    """读取 PDF，生成并缓存 PageIndex 索引，返回文档 ID。"""
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF 文件不存在：{pdf_path}")
    if pdf_path.suffix.lower() != ".pdf":
        raise ValueError("输入文件必须是 PDF")

    client = PageIndexClient(
        index="deepseek/deepseek-chat",
        chat="deepseek/deepseek-chat",
        storage_path=str(storage_path),
    )
    result = client.submit_document(str(pdf_path), wait=True)
    return result["doc_id"]


def main() -> None:
    parser = argparse.ArgumentParser(description="使用 PageIndex 为 PDF 建立索引")
    parser.add_argument("pdf", type=Path, help="PDF 文件路径")
    parser.add_argument(
        "--storage-path",
        type=Path,
        default=Path(".pageindex"),
        help="索引缓存目录，默认：.pageindex",
    )
    args = parser.parse_args()

    doc_id = build_index(args.pdf, args.storage_path)
    print(f"索引完成，doc_id={doc_id}")
    print(f"索引缓存：{args.storage_path.resolve()}")


if __name__ == "__main__":
    main()
