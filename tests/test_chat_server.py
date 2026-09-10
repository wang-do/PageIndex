from fastapi.testclient import TestClient

import chat_server
from conversation_history import ConversationHistory


def test_session_endpoints_and_first_question_title(tmp_path, monkeypatch):
    test_history = ConversationHistory(tmp_path / "conversation_history.db")
    monkeypatch.setattr(chat_server, "history", test_history)
    monkeypatch.setattr(chat_server, "ask_question", lambda *args, **kwargs: "模拟回答")
    client = TestClient(chat_server.app)

    created = client.post("/api/sessions").json()
    session_id = created["session_id"]
    assert client.get("/api/sessions").json()[0]["title"] == "新会话"

    answer = client.post("/api/chat", json={
        "session_id": session_id,
        "question": "住宅卧室净高有什么要求？",
    }).json()
    assert answer == {
        "answer": "模拟回答",
        "title": "住宅卧室净高有什么要求？",
    }

    renamed = client.patch(
        f"/api/sessions/{session_id}",
        json={"title": "住宅净高"},
    )
    assert renamed.json() == {"title": "住宅净高"}

    deleted = client.delete(f"/api/sessions/{session_id}")
    assert deleted.json() == {"deleted": True}
    assert client.get("/api/sessions").json() == []


def test_frontend_is_served(tmp_path, monkeypatch):
    monkeypatch.setattr(
        chat_server,
        "history",
        ConversationHistory(tmp_path / "conversation_history.db"),
    )
    response = TestClient(chat_server.app).get("/")

    assert response.status_code == 200
    assert "正在检索" in response.text
    assert "session-list" in response.text
    assert "session-menu" in response.text
    assert "dialog-backdrop" in response.text


def test_chat_returns_json_when_model_call_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(
        chat_server,
        "history",
        ConversationHistory(tmp_path / "conversation_history.db"),
    )
    monkeypatch.setattr(
        chat_server,
        "ask_question",
        lambda *args, **kwargs: (_ for _ in ()).throw(ConnectionError("网络不可用")),
    )
    client = TestClient(chat_server.app)
    session_id = client.post("/api/sessions").json()["session_id"]

    response = client.post("/api/chat", json={
        "session_id": session_id,
        "question": "测试问题",
    })

    assert response.status_code == 502
    assert response.json()["detail"] == "模型服务调用失败：网络不可用"
