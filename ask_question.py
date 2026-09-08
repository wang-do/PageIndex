"""使用已有的 PageIndex 索引提问。"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Optional

from pageindex import PageIndexLocalClient


DEFAULT_QUESTION = "栏杆净间距有什么要求？"


def ask_question(
    doc_ids: Optional[list[str]], question: str, storage_path: Path
) -> str:
    """调用 PageIndex 原生 responses 接口，返回检索路径和回答。"""
    client = PageIndexLocalClient(
        index_model="deepseek/deepseek-chat",
        chat_model="deepseek-chat",
        storage_path=str(storage_path),
    )
    response = client.responses(question, doc_id=doc_ids, max_turns=5)

    print("检索路径：")
    for item in response["items"]:
        if item.get("type") == "function_call":
            print(f"- {item['name']}({item['arguments']})")

    final_message = response["output"][-1]
    answer = final_message["content"][0]["text"]
    return answer


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
