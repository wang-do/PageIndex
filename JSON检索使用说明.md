# PageIndex JSON 检索使用说明

## 1. 配置

在 `PageIndex-main/.env` 配置 DeepSeek 的 OpenAI 兼容接口：

```env
OPENAI_API_KEY=你的密钥
OPENAI_BASE_URL=https://api.deepseek.com
```

## 2. 安装需要使用的.venv环境

vscode中右下角 select interpret 然后找到.venv中的python.exe加载即可

## 3. 导入 JSON

### 命令行

在 `PageIndex-main` 目录执行：

```powershell
.\.venv\Scripts\python.exe import_structure_json.py .\demo_building_code_pageindex.json
```

命令输出示例：

```text
导入完成，doc_id=pi-xxxxxxxx
```

`doc_id` 是后续提问时使用的文档 ID。索引数据默认写入 `.pageindex`。



### Python run：

直接运行 import_structure_json.py



## 4. 提问并查看检索路径

### 命令行

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



### Python run：

直接运行 ask_question.py
