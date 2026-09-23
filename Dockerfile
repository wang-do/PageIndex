FROM python:3.11-slim

WORKDIR /app

# 依赖层（代码变更时缓存复用）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 应用代码（pdf/ 与 .pageindex/ 由卷挂载提供，不进镜像）
COPY chat_server.py ask_question.py clause_index.py reference_merge.py retrieval_cache.py conversation_history.py ./
COPY pageindex/ ./pageindex/
COPY frontend/ ./frontend/

# 数据打包进镜像：key、PDF、索引（单包部署，服务器无需额外文件）
COPY .env /app/.env
COPY pdf/ /app/pdf/
COPY .pageindex/ /app/.pageindex/

ENV PAGEINDEX_PDF_DIR=/app/pdf     PYTHONUNBUFFERED=1     TZ=Asia/Shanghai

EXPOSE 8000

CMD ["uvicorn", "chat_server:app", "--host", "0.0.0.0", "--port", "8000"]
