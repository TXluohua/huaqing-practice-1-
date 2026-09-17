# 半导体设备维护知识库智能问答系统

面向半导体设备维护的知识库问答系统。核心命题：**查得准、引得实、不敢答的敢说不知道**。

完整设计见 `半导体设备维护知识库智能问答系统_开发文档（初稿）(2).md`，
接口契约见 `接口文档.md`，分工与协作约定见 `后端二人分工与协作规范.md`。

---

## 当前状态

**端到端已跑通**：LangGraph 九节点主链路 + RAG 检索生成 + FastAPI 接口层 + Vue3 前端。
**单测 84 passed；后端端到端 58 项断言全过。**

| 层 | 状态 | 位置 |
| --- | --- | --- |
| 状态契约 | ✅ | `backend/agents/state.py` |
| 主图（9 节点 + 条件边） | ✅ | `backend/agents/graph.py`、`backend/agents/nodes/` |
| 检索与生成（RAG） | ✅ | `backend/rag/`、`backend/tools/kb_tools.py` |
| 跨源检索（MCP 工单） | ✅ | `backend/mcp_servers/ticket_server.py`、`backend/mcp_client.py` |
| 接口层（11 个接口 + SSE） | ✅ | `backend/routers/` |
| 服务层 | ✅ | `backend/services/{chat_service,kb_service}.py` |
| 业务库与会话检查点 | ✅ | `backend/db.py`（业务）、`backend/memory.py`（检查点） |
| 应用入口与预热 | ✅ | `backend/main.py`、`backend/dependency.py` |
| 部署产物 | ✅ | `deploy/`、`docker-compose.yml`（nginx 已关 SSE 缓冲） |
| 前端 | ✅ | `frontend/`（Vue3 + Vite + Element Plus） |

**尚未完成**：图片知识侧入库（抽图无 VLM caption）、docx/pptx/xlsx 解析（需 MarkItDown）、
检索层权限过滤规则（缺权限分级表）、黄金集扩样与正式评测报告。

> 逐项完成度与实测证据见 **`功能完成度清单.md`**。

---

## 快速开始

> **密钥**：若 `DEEPSEEK_API_KEY` / `DASHSCOPE_API_KEY` 写在 `~/.bashrc`，
> 注意非交互 shell 读不到，需用 `bash -ic 'make dev'` 启动。
> （`.env` 中的环境变量优先级低于系统环境变量，两者可共存。）

---

## 快速开始

### 1. 准备后端环境

```bash
make install          # python3 -m venv .venv && pip install -r requirements.txt
```

### 2. 准备模型并建索引

检索质量依赖本地模型（**约 4.6GB，不入库**）：

```bash
.venv/bin/python scripts/prepare_model.py --check          # 看模型是否就绪
.venv/bin/python scripts/prepare_model.py --download --source modelscope --with-reranker
make ingest                                                # 建索引（5 文档 / 32 切片）
```

> 本机没有模型时，可临时用零依赖兜底跑通全链路（检索质量下降，日志会告警）：
> `EMBEDDING_PROVIDER=hashing RERANK_PROVIDER=lexical make ingest`

### 3. 启动服务

```bash
make dev              # 后端 http://127.0.0.1:8000  （接口文档 /docs）
make install-web      # 首次需要：安装前端依赖
make dev-web          # 前端 http://127.0.0.1:5173
```

浏览器打开 **http://127.0.0.1:5173** 即可问答。前端通过 Vite 代理把 `/api` 与 `/static`
转发到 `http://localhost:8000`，无需额外配置跨域。

### 4. 配置密钥（可选但强烈建议）

```bash
cp .env.example .env
```

| 变量 | 作用 | 未配置时 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | 文本生成 | 降级为**抽句式答案**，要点命中率明显下降 |
| `DASHSCOPE_API_KEY` | 图片识别（qwen-vl-max） | 多模态提问不可用，提示改用文字 |

---

## 常用命令

```bash
make test                                  # 全量测试（84 个用例）
make ask Q="刻蚀机腔体真空度异常怎么排查？"   # 命令行单问
make ask-stream Q="参数下限是多少"           # 流式链路
make clean                                 # 清理字节码与测试缓存
```

---

## 目录结构

```
backend/
├── main.py            # FastAPI 入口 + lifespan 预热
├── setting.py         # 全部配置（调参不改代码）
├── schemas.py         # 请求/响应契约 + ServiceError
├── dependency.py      # 启动预热与进程级单例
├── db.py              # 业务库模型（SQLite 开发 / MySQL 生产）
├── memory.py          # 会话检查点（SqliteSaver）+ session↔thread 映射
├── routers/           # 路由层：只做 HTTP 翻译
├── services/          # 服务层：会话/问答/入库/索引
├── agents/            # LangGraph：state / graph / factory / nodes / prompts
├── rag/               # 解析、切片、向量库、混合检索、精排、引用校验
├── tools/             # 工具（检索、术语、图片识别、备件查询）
└── data/              # 运行时数据（索引、模型、上传、检查点；不入 git）
frontend/              # Vue3 + Vite + Element Plus
data/
├── raw/               # 入库语料
└── eval/              # 黄金集与 badcase
```

依赖方向（开发文档 6.1）：`routers → services → agents/tools/rag → config`，禁止反向 import。

---

## 关键机制

| 机制 | 说明 |
| --- | --- |
| **启动预热** | `lifespan` 预加载 embedding 与精排（实测约 10.2s）。不预热会把这段时间算进第一个请求的首 Token，顶穿 NFR-01（首 Token ≤ 2s） |
| **降级不中断** | 模型/MCP/数据库任一不可用都不阻止启动，由 `GET /api/health` 如实报 `degraded` 并给出原因 |
| **SSE 事件** | `meta → image? → token* → citations → done`，失败发 `error`；契约见 `接口文档.md` §6 |
| **token 缓冲** | token 在 `verify` 定稿后才发：verify 拒答时会改写答案，若已流式发出，用户会先看到操作步骤再看到拒答 |
| **零幻觉** | 引用由代码分配、verify 独立校验、证据不足即拒答；备件替代无依据时明确「需原厂确认」 |
