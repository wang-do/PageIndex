"""从法规 JSON 的章节标题生成 jieba 自定义词典（userdict.txt）。

用法：python build_userdict.py
输入：../json/*.json 的 clauses[].levels[].title
输出：userdict.txt（与 retrieval_cache.py 同目录，格式：词 频率 词性）
"""

import json
import re
from pathlib import Path

JSON_DIR = Path(__file__).parent.parent / "json"
OUTPUT_PATH = Path(__file__).parent / "userdict.txt"

# 通用意图词，保证"有什么规定"和"能不能改"这类问题不会抽成同一个词串。
# "总则"等是法规文档结构词（jieba 通用词典里没有，必须显式收进）。
INTENT_WORDS = (
    "规定", "要求", "标准", "条件", "范围", "情形", "定义", "限制",
    "有哪些", "是多少", "是否", "能否", "哪些", "总则",
)


def clean_title(title: str) -> str:
    """去掉编号前缀、空白与纯符号，只留可作为术语的标题文本。"""
    text = re.sub(r"[\s\u3000]+", "", title)
    text = re.sub(r"^[0-9.、()（）\-]+", "", text)
    return text


def main() -> None:
    terms: set[str] = set()
    for json_file in sorted(JSON_DIR.glob("*.json")):
        data = json.loads(json_file.read_text(encoding="utf-8"))
        for clause in data.get("clauses", []):
            for level in clause.get("levels", []):
                title = clean_title(level.get("title") or "")
                # 术语至少 2 个汉字，排除纯数字/单字/过长标题。
                if len(title) >= 2 and len(title) <= 12 and re.search(r"[\u4e00-\u9fff]", title):
                    terms.add(title)
    terms.update(INTENT_WORDS)

    lines = sorted(f"{term} 10 n" for term in terms)
    OUTPUT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"已生成 {OUTPUT_PATH.name}：{len(lines)} 个词条")


if __name__ == "__main__":
    main()
