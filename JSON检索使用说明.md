# PageIndex JSON 检索使用说明

本方案不读取 PDF。将已有的目录树和页面原文 JSON 导入 PageIndex 本地存储后，仍使用 PageIndex 源码的 `responses()`、`get_document_structure` 和 `get_page_content` 完成问答。

## 1. 配置

在 `PageIndex-main/.env` 配置 DeepSeek 的 OpenAI 兼容接口：

```env
OPENAI_API_KEY=你的密钥
OPENAI_BASE_URL=https://api.deepseek.com
```

## 2. JSON 格式

JSON 必须包含以下字段：

```json
{
  "doc_name": "法规名称或唯一标识",
  "doc_description": "法规简介",
  "pages": [
    {"page_index": 1, "markdown": "第 1 页原文"}
  ],
  "structure": [
    {
      "title": "第 1 章",
      "node_id": "0000",
      "start_index": 1,
      "end_index": 1,
      "summary": "章节摘要",
      "nodes": []
    }
  ]
}
```

- `doc_name` 只是文档标识，不代表 PDF 文件，也无需使用 `.pdf` 后缀。
- `pages` 是原文来源；`page_index` 必须从 1 开始连续递增。
- `structure` 是层级目录；每个节点必须包含 `title`、`node_id`、`start_index`、`end_index`。
- `summary` 用于目录粗定位；`start_index/end_index` 指向 `pages` 的页码范围。

可直接参考 `demo_building_code_pageindex.json`。

## 3. 导入 JSON

在 `PageIndex-main` 目录执行：

```powershell
.\.venv\Scripts\python.exe import_structure_json.py .\demo_building_code_pageindex.json
```

命令输出示例：

```text
导入完成，doc_id=pi-xxxxxxxx
```

`doc_id` 是后续提问时使用的文档 ID。索引数据默认写入 `.pageindex`。

## 4. 提问并查看检索路径

```powershell
.\.venv\Scripts\python.exe ask_question.py "pi-xxxxxxxx" "栏杆净间距有什么要求？"
```

输出包含 PageIndex 的实际工具调用路径，例如：

```text
检索路径：
- get_document_structure({"doc_name": "demo_building_code_json"})
- get_page_content({"doc_name": "demo_building_code_json", "pages": "5"})

栏杆垂直杆件间的净间距不应大于 0.11m。
```

含义：模型先读取 JSON 的 `structure` 定位目录，再根据节点页码从 JSON 的 `pages` 读取第 5 页原文，最后回答。

## 5. 指定索引目录

导入和提问必须使用同一个 `--storage-path`：

```powershell
.\.venv\Scripts\python.exe import_structure_json.py .\your_document.json --storage-path .\json_index
.\.venv\Scripts\python.exe ask_question.py "pi-xxxxxxxx" "你的问题" --storage-path .\json_index
```

## 6. 当前边界

- JSON 缺少 `pages` 时，PageIndex 无法在定位后读取原文，因此不能导入。
- 目录树的页码超出 `pages` 范围时，导入会拒绝。
- 最终回答是否严格只引用原文，取决于模型指令；当前路径会展示模型实际读取了哪些页面。
