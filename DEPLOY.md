# PageIndex 法规RAG —— Docker 部署指南

## 文件清单

| 文件 | 作用 |
|---|---|
| Dockerfile | 镜像构建（python:3.11-slim + 依赖 + 应用代码）|
| docker-compose.yml | 编排（端口 8000、数据卷、.env 配置）|
| requirements.txt | Python 依赖清单 |
| .dockerignore | 构建时排除 pdf/.pageindex/.venv 等 |

## 设计要点

- **单端口**：uvicorn 同时服务前端页面（/）和全部 API（/api/chat、/api/knowledge-base 等），浏览器访问 `http://服务器IP:8000` 即可用
- **数据不进镜像**：pdf/（46 本规范）、.pageindex/（索引+缓存+历史）通过**卷挂载**提供——镜像只有代码，重建/升级镜像数据无损
- **配置**：.env（DEEPSEEK_API_KEY / OPENAI_API_KEY / OPENAI_BASE_URL）以只读方式挂载进容器

## Linux 部署步骤

### 1. 上传项目到 Linux

```bash
scp -r PageIndex-main user@服务器IP:/opt/pageindex
```

### 2. 进入目录，确认数据齐全

```bash
cd /opt/pageindex
# 应看到：pdf/（46本PDF）、.pageindex/（索引）、.env、Dockerfile、docker-compose.yml
```

### 3. 构建镜像（首次约 2~5 分钟）

```bash
docker compose build
```

### 4. 启动（后台常驻）

```bash
docker compose up -d
```

### 5. 验证

```bash
curl http://localhost:8000/api/knowledge-base
```

浏览器访问 `http://服务器IP:8000` —— 即为完整前端。

## 常用运维命令

```bash
docker compose logs -f     # 看日志
docker compose restart     # 重启
docker compose down        # 停止
docker compose build && docker compose up -d   # 代码更新后重建
```

## 注意事项

- 端口冲突：编辑 docker-compose.yml 的 ports（如 "80:8000" 可直接用 80 端口）
- 防火墙：放行 8000 端口（ufw allow 8000，或云服务器安全组规则）
- .env 里的 API key 是 DeepSeek 的，服务器需能访问 api.deepseek.com
- Windows 上先构建测试：本机装 Docker Desktop 后 `docker compose up -d` 同样可用
- 数据备份：.pageindex/ 目录即全部运行数据（索引+缓存+历史），定期拷贝即可
