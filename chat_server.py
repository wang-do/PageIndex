"""法规问答的最小 FastAPI 服务。"""

import json
import io
import os
import re
import threading
from pathlib import Path
from typing import Iterator
from uuid import uuid4

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse, Response
from pydantic import BaseModel

from ask_question import _INSTRUCTION_ZH, ask_question
from conversation_history import ConversationHistory


FRONTEND_FILE = Path(__file__).parent / "frontend" / "index.html"
STORAGE_PATH = Path(".pageindex")
# 原始法规 PDF 目录。可通过环境变量覆盖，便于部署到其他机器。
PDF_DIR = Path(os.getenv(
    "PAGEINDEX_PDF_DIR",
    str(Path(__file__).resolve().parent / "pdf"),
)).expanduser().resolve()
history = ConversationHistory(STORAGE_PATH / "conversation_history.db")

app = FastAPI()


def _normalise_doc_name(value: str) -> str:
    """将法规编号规范化，用于安全、大小写不敏感的文件名匹配。"""
    return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", value or "").lower()


def _find_pdf(spec_no: str) -> Path:
    """只在白名单 PDF_DIR 内按 spec_no 查找 PDF，避免任意路径读取。

    优先精确匹配；不命中时退化为前缀匹配（双向），以兼容引用数据
    与文件名之间修订年份后缀的差异，例如 spec_no 为 GB50016-2014
    而文件名为 GB50016-2014(2018).pdf。"""
    if not PDF_DIR.is_dir():
        raise HTTPException(status_code=404, detail=f"PDF目录不存在: {PDF_DIR}")
    target = _normalise_doc_name(Path(spec_no).stem)
    if not target:
        raise HTTPException(status_code=404, detail=f"未找到法规 PDF: {spec_no}")
    prefix_hits: list[Path] = []
    for candidate in PDF_DIR.glob("*.pdf"):
        name = _normalise_doc_name(candidate.stem)
        if name == target:
            return candidate.resolve()
        if name.startswith(target) or target.startswith(name):
            prefix_hits.append(candidate)
    if len(prefix_hits) == 1:
        return prefix_hits[0].resolve()
    if len(prefix_hits) > 1:
        # 多个候选时取编号最长（信息最全）的那个，保证行为确定。
        return max(prefix_hits, key=lambda p: len(p.stem)).resolve()
    raise HTTPException(status_code=404, detail=f"未找到法规 PDF: {spec_no}")


@app.get("/api/documents/{spec_no}.pdf")
def document_pdf(spec_no: str) -> Response:
    """按法规编号返回原始 PDF。"""
    pdf_path = _find_pdf(spec_no)
    # inline 才能在浏览器/iframe 内直接打开并支持 #page=N 跳页，attachment 会强制下载。
    return FileResponse(
        pdf_path,
        media_type="application/pdf",
        filename=pdf_path.name,
        content_disposition_type="inline",
    )


@app.get("/api/reference-image")
def reference_image(
    spec_no: str,
    page_pdf: int,
    bbox: str | None = None,
    img_w: int | None = None,
    img_h: int | None = None,
) -> Response:
    """渲染 PDF 指定页，并按原始 bbox 叠加红框，返回 PNG。"""
    if page_pdf < 1:
        raise HTTPException(status_code=422, detail="page_pdf 必须从 1 开始")
    pdf_path = _find_pdf(spec_no)
    try:
        import pypdfium2 as pdfium
        from PIL import Image, ImageDraw
    except ImportError as exc:
        raise HTTPException(
            status_code=500,
            detail="缺少 PDF 渲染依赖，请执行 .\\.venv\\Scripts\\python.exe -m pip install pypdfium2 Pillow",
        ) from exc

    try:
        document = pdfium.PdfDocument(str(pdf_path))
        if page_pdf > len(document):
            raise HTTPException(status_code=404, detail=f"PDF只有 {len(document)} 页")
        page = document[page_pdf - 1]
        bitmap = page.render(scale=2.0)
        image = bitmap.to_pil()
        draw = ImageDraw.Draw(image)
        if bbox:
            values = json.loads(bbox)
            if not isinstance(values, list) or len(values) != 4:
                raise ValueError("bbox 应为 [x1,y1,x2,y2]")
            x1, y1, x2, y2 = [float(v) for v in values]
            source_w = float(img_w or image.width)
            source_h = float(img_h or image.height)
            sx, sy = image.width / source_w, image.height / source_h
            rect = [round(x1 * sx), round(y1 * sy), round(x2 * sx), round(y2 * sy)]
            # 兼容异常坐标。
            rect = [
                max(0, min(image.width - 1, rect[0])),
                max(0, min(image.height - 1, rect[1])),
                max(0, min(image.width - 1, rect[2])),
                max(0, min(image.height - 1, rect[3])),
            ]
            # 条文级背景高亮：半透明粉底 + 红边框（规范阅读器同款效果）。
            overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
            odraw = ImageDraw.Draw(overlay)
            odraw.rectangle(rect, fill=(255, 178, 178, 88), outline=(255, 32, 32, 255), width=4)
            image = Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")
            draw = ImageDraw.Draw(image)
            draw.rectangle(rect, outline="#ff2020", width=2)
        output = io.BytesIO()
        image.save(output, format="PNG")
        # 响应头带总页数（前端翻页显示"第 N / M 页"用）。
        return Response(output.getvalue(), media_type="image/png", headers={
            "Cache-Control": "no-store",
            "X-Page-Count": str(len(document)),
            "X-Page-Index": str(page_pdf),
        })
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"PDF页面或bbox无效: {exc}") from exc
################################上述是对pdf的接口###################################

################################知识库浏览接口#######################################

@app.get("/api/knowledge-base")
def knowledge_base_list() -> Response:
    """列出全部知识库文档：名称、简介、元数据、页数与 PDF 可用性。"""
    docs_dir = STORAGE_PATH / "docs"
    items = []
    if docs_dir.is_dir():
        for doc_dir in sorted(docs_dir.iterdir()):
            doc_file = doc_dir / "doc.json"
            if not doc_file.exists():
                continue
            try:
                doc = json.loads(doc_file.read_text(encoding="utf-8"))
            except (TypeError, ValueError):
                continue
            name = doc.get("name") or doc_dir.name
            pages_file = doc_dir / "pages.json"
            page_count = 0
            if pages_file.exists():
                try:
                    page_count = len(json.loads(pages_file.read_text(encoding="utf-8")))
                except (TypeError, ValueError):
                    pass
            has_pdf = False
            try:
                _find_pdf(name)
                has_pdf = True
            except HTTPException:
                has_pdf = False
            meta = doc.get("metadata") or {}
            items.append({
                "doc_id": doc_dir.name,
                "name": name,
                "description": doc.get("description") or "",
                "spec_no": str(meta.get("spec_no", "")).replace(" ", ""),
                "spec_name": meta.get("spec_name") or "",
                "status": meta.get("status") or "",
                "implement_date": meta.get("implement_date") or "",
                "page_count": page_count,
                "has_pdf": has_pdf,
            })
    # 有 PDF 的排前，其余按名称排序。
    items.sort(key=lambda x: (not x["has_pdf"], x["name"]))
    return Response(json.dumps(items, ensure_ascii=False), media_type="application/json")


@app.get("/api/knowledge-base/{doc_id}/first-page")
def knowledge_base_first_page(doc_id: str, page: int = 1) -> Response:
    """渲染知识库对应 PDF 的指定页（详情视图预览/翻页用），默认第一页。"""
    doc_file = STORAGE_PATH / "docs" / doc_id / "doc.json"
    if not doc_file.exists():
        raise HTTPException(status_code=404, detail="知识库文档不存在")
    doc = json.loads(doc_file.read_text(encoding="utf-8"))
    pdf_path = _find_pdf(doc.get("name") or doc_id)
    if page < 1:
        raise HTTPException(status_code=422, detail="page 必须从 1 开始")
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:
        raise HTTPException(status_code=500, detail="缺少 PDF 渲染依赖") from exc
    document = pdfium.PdfDocument(str(pdf_path))
    if page > len(document):
        raise HTTPException(status_code=404, detail=f"PDF 只有 {len(document)} 页")
    pg = document[page - 1]
    image = pg.render(scale=2.0).to_pil()
    output = io.BytesIO()
    image.save(output, format="PNG")
    return Response(output.getvalue(), media_type="image/png", headers={
        "Cache-Control": "no-store",
        "X-Page-Count": str(len(document)),
        "X-Page-Index": str(page),
    })

######################上述是对pdf的接口###################################
####################################################################################

@app.on_event("startup")
def warmup() -> None:
    """启动时后台预热，把冷启动成本（jieba/SDK/连接/prompt 缓存）
    从用户第一次提问挪到服务启动阶段。预热失败不影响服务。"""

    def _warm() -> None:
        try:
            import retrieval_cache as rc
            from pageindex import PageIndexLocalClient

            rc._ensure_jieba()
            client = PageIndexLocalClient(
                index_model="deepseek/deepseek-chat",
                chat_model="deepseek-chat",
                storage_path=str(STORAGE_PATH),
            )
            client.list_documents()
            # 一次最小模型请求：热 HTTPS 连接 + agents SDK 初始化 +
            # 建立系统提示前缀的 prompt 缓存。
            client.responses("ping", max_turns=1, instructions=_INSTRUCTION_ZH)
            print("[预热] 完成")
        except Exception as exc:
            print(f"[预热] 失败（不影响服务）：{exc}")

    threading.Thread(target=_warm, daemon=True).start()


class ChatRequest(BaseModel):
    """一轮聊天请求。"""

    session_id: str
    question: str
    doc_ids: list[str] | None = None  # 知识库选中的 doc_id；None=检索全部


class RenameRequest(BaseModel):
    """会话重命名请求。"""

    title: str


@app.get("/")
def index() -> FileResponse:
    """返回聊天页面。"""
    return FileResponse(FRONTEND_FILE)


@app.get("/marked.min.js")
def marked_js() -> FileResponse:
    """返回前端 markdown 渲染库。"""
    return FileResponse(FRONTEND_FILE.parent / "marked.min.js")


@app.post("/api/sessions")
def create_session() -> dict[str, str]:
    """创建一个互不共享历史的新会话。"""
    session_id = uuid4().hex
    history.create_session(session_id)
    return {"session_id": session_id, "title": "新会话"}


@app.get("/api/sessions")
def list_sessions() -> list[dict[str, str]]:
    """返回已有会话。"""
    return history.list_sessions()


@app.get("/api/sessions/{session_id}/messages")
def get_messages(session_id: str) -> list[dict]:
    """返回指定会话的历史消息。"""
    return history.get_messages(session_id)


@app.patch("/api/sessions/{session_id}")
def rename_session(session_id: str, request: RenameRequest) -> dict[str, str]:
    """修改指定会话的标题。"""
    title = request.title.strip()
    if not title:
        raise HTTPException(status_code=422, detail="会话名称不能为空")
    if not history.rename_session(session_id, title[:30]):
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"title": title[:30]}


@app.delete("/api/sessions/{session_id}")
def delete_session(session_id: str) -> dict[str, bool]:
    """删除指定会话及其历史消息。"""
    if not history.delete_session(session_id):
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"deleted": True}


@app.post("/api/chat")
def chat(request: ChatRequest) -> StreamingResponse:
    """在指定会话中继续提问，SSE 逐步推送检索过程与答案。"""
    question = request.question.strip()
    session_id = request.session_id.strip()
    if not question or not session_id:
        raise HTTPException(status_code=422, detail="问题和会话不能为空")
    doc_ids = request.doc_ids  # None=检索全部；list=仅检索选中的知识库
    title = history.set_initial_title(session_id, question)

    def sse() -> Iterator[str]:
        try:
            for event in ask_question(doc_ids, question, STORAGE_PATH,
                                      session_id=session_id):
                if event.get("type") == "done":
                    event["title"] = title
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception as exc:
            # 生成器任何未捕获异常也转成 error 事件，避免流中断后前端无提示。
            detail = f"模型服务调用失败：{exc}"
            yield f"data: {json.dumps({'type': 'error', 'detail': detail}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        sse(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
