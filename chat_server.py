"""法规问答的最小 FastAPI 服务。"""

import json
from pathlib import Path
from typing import Iterator
from uuid import uuid4

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from ask_question import ask_question
from conversation_history import ConversationHistory


FRONTEND_FILE = Path(__file__).parent / "frontend" / "index.html"
STORAGE_PATH = Path(".pageindex")
history = ConversationHistory(STORAGE_PATH / "conversation_history.db")

app = FastAPI()


class ChatRequest(BaseModel):
    """一轮聊天请求。"""

    session_id: str
    question: str


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
    title = history.set_initial_title(session_id, question)

    def sse() -> Iterator[str]:
        try:
            for event in ask_question(None, question, STORAGE_PATH,
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
