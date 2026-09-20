# 半导体设备维护知识库智能问答系统

面向半导体设备维护的知识库问答系统。核心命题：**查得准、引得实、不敢答的敢说不知道**。

完整设计见 `半导体设备维护知识库智能问答系统_开发文档（初稿）(2).md`，
接口契约见 `接口文档.md`，分工与协作约定见 `后端二人分工与协作规范.md`。

---

## 当前状态

**端到端已跑通**：LangGraph 九节点主链路 + RAG 检索生成 + FastAPI 接口层 + Vue3 前端。
**单测 139 passed；接口层 35 个接口全通（含新增的业务接口 24 个）；健康检查 7 个组件全 ok。**

| 层 | 状态 | 位置 |
| --- | --- | --- |
| 状态契约 | ✅ | `backend/agents/state.py` |
| 主图（9 节点 + 条件边） | ✅ | `backend/agents/graph.py`、`backend/agents/nodes/` |
| 检索与生成（RAG） | ✅ | `backend/rag/`、`backend/tools/kb_tools.py` |
| 跨源检索（MCP 工单） | ✅ | `backend/mcp_servers/ticket_server.py`、`backend/mcp_client.py` |
| 接口层（35 个接口 + SSE） | ✅ | `backend/routers/` |
| 服务层 | ✅ | `backend/services/{chat_service,kb_service,plan_service,parts_service,training_service}.py` |
| 业务库与会话检查点 | ✅ | `backend/db.py`（12 张表）、`backend/memory.py`（检查点） |
| 应用入口与预热 | ✅ | `backend/main.py`、`backend/dependency.py` |
| 部署产物 | ✅ | `deploy/`、`docker-compose.yml`（nginx 已关 SSE 缓冲） |
| 前端（问答主链路） | ✅ | `frontend/`（Vue3 + Vite + Element Plus）：`/chat`、`/history`、`/admin` |
| 前端（三块新业务） | ✅ 独立页面已就位，⏳ 打磨待第三人 | **独立路由** `/plans`、`/parts`、`/procurement`、`/training`，顶栏与问答分组并列，见 `前端对接说明（三块新业务）.md` |

### 实测指标（真实 LLM + 真实 VLM，2026-09-20）

| 编号 | 指标 | 目标 | 实测 |
| --- | --- | --- | --- |
| G1 | Top-5 检索命中率 | ≥ 80% | **100%**（45 条文本黄金集） |
| G2 | 引用覆盖率 | **100%（不可降级）** | **100%** |
| G2 | 引用真实性 | ≥ 95% | **100%**（270 项引用、0 伪造） |
| G3 | 要点命中率（严格口径 / 宽松口径） | ≥ 80% | **89.7%** / **100%**（185/185 要点；未配 LLM 的抽句式降级值为 44.7%） |
| G3 | 无依据拒答率 | ≥ 90% | **100%**（5/5 负样本） |
| G4 | 图片关键信息提取与作答 | ≥ 85% | **5/5 通过**（含 1 条按设计拒答的模糊纸条） |
| G5 | 文字 P95 / 带外部源 P95 | ≤ 8s / ≤ 15s | 空闲环境 **3.9s / 4.3s**；同机并行跑演示服务与子任务时 24.0s / 23.3s |
| — | 误拒正样本 | 0 | **0 / 40** |
| — | MRR / NDCG@5 | — | 0.9750 / 0.9785 |
| — | 引用归属（LLM 路径） | — | 自动补引用 45 句 / 因无依据省略 21 句（省略会在拒答原因里说明） |

> 复跑方式见下方「评测与验收」。逐项证据见 `功能完成度清单.md`，交付清单见 `交付说明.md`。

**尚未完成**：三块新业务的**前端页面**（后端已就绪，交接见 `前端对接说明（三块新业务）.md`）、
图片知识侧入库（抽图无 VLM caption）、docx/pptx/xlsx 解析（需 MarkItDown）、
检索层权限过滤规则（缺权限分级表）、会话删除 / 重命名。

**已知限制（2026-09-18 实测，详见 `功能完成度清单.md` §2.2b 与 `data/eval/badcases.md`）**：

| 限制 | 实测 | 影响 / 处置 |
| --- | --- | --- |
| **并发不提升吞吐** | n=1 → 8.5s、n=5 → 40s、n=20 → 190s，吞吐恒定 ≈0.11 req/s | 不能支撑 20 并发（NFR-02）。主因：CPU 上 Cross-Encoder 精排 ≈4.9s/问且串行。优化路径：外部查询与精排并行（省 ~4s）、GPU 部署、`rerank_max_candidates` 12→8 |
| 检索层不做权限过滤 | `user_role` 已透传但未参与过滤 | 检索即可见全部语料；需先定义权限分级表（FR-11） |
| 阈值标定样本偏小 | 精排 0.5 / 术语缺失比 0.40 / 引用归属 0.45 仅在 50 条上校准 | 真实语料规模变化后建议重标（改 `setting.py` 即可，不改代码） |
| 工单检索是关键词重合 | 无语义召回，无词重合即返回空 | 需要时改向量召回 |
| 图片知识侧不入库 | `kb_image` 表 0 行，抽图不写 caption | 图纸/参数表截图不能**被检索到**（但作为**提问图片**已完全可用） |

> 逐项完成度与实测证据见 **`功能完成度清单.md`**。

---

## 三块新业务（后端已完成，前端待第三人）

原开发文档 §1.3 列为「不做 / 后续批次」的三块功能，按项目方要求补齐**后端**：

| 功能 | 端点数 | 服务层 | 数据表 | 一句话 |
| --- | --- | --- | --- | --- |
| 维护计划生成 | 4 | `backend/services/plan_service.py` | `maintenance_plan` | 按设备 + 运行数据，从手册**原文**抽周期算出到期项；查不到周期的如实进 `uncovered` |
| 备件商城与采购 | 14 | `backend/services/parts_service.py` | `part_order`、`part_settlement` | 商城目录 → **购物车（= 草稿单，可加/改/删）** → 提交 → 人工确认 → 收货 → 结算台账 |
| 考核认证 | 6 | `backend/services/training_service.py` | `training_quiz`、`training_attempt`、`certification` | 按设备出题（每题带依据）→ 判分 → 显式发证 → 到期提醒 |

**与问答完全独立**（项目方明确要求，且已落地为可执行的检查）：

- 接口：各有自己的前缀 `/api/plans`、`/api/parts`、`/api/training`，与 `/api/chat` 不相交，
  不需要会话、`qa_id` 或 SSE；
- 代码：问答链路不 import 这三块业务，三块业务也不 import 问答链路
  （`tests/test_api/test_module_independence.py` 用 AST 检查 import，越界即失败）；
- 页面：四个独立路由 + 顶栏独立分组，业务页不引用 `@/api/chat` / `@/utils/sse` / `@/stores/*`。

> 注意区分：问答页里的「备件问答」与「`mode=training` 分层讲解」是**问答能力**；
> 商城下单、采购结算、出题发证这些**业务操作**只在独立页面里做。

三条红线（**代码层强制**，不是文档约定）：

1. **不对供应商发起真实下单** —— 只生成内部采购申请单，`approve` 即人工确认点
   （购物车就是草稿单，提交后明细即冻结）；
2. **替代件必须有兼容性依据** —— 台账里查不到 `basis` 直接 `400 SUBSTITUTE_BASIS_REQUIRED`；
   禁止替代清单会随备件一起返回；
3. **不自动发证** —— 判分接口的 `certification_id` 恒为 `null`，发证须显式调用并指定等级与
   有效期，证书固定标注「内部授权，不代表设备原厂认证」。

零幻觉同样落到这三块：计划项的周期值只能从检索到的原文里正则抽取，每条带 `evidence`；
出题前先检索，LLM 生成的题目若 `evidence` 越界或引用了不在证据里的数字则**丢弃该题**，
一题都出不来就返回 `422 QUIZ_GENERATION_FAILED`（宁可不命题，也不出无依据的题）。

- 接口清单：`接口文档.md` §10
- 前端对接（请求 / 响应 / 按钮状态机 / 演示脚本）：`前端对接说明（三块新业务）.md`

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

> **推荐用 `.env`**：非交互 shell 也能读到（写在 `~/.bashrc` 里的变量在
> `make dev` 这类非交互启动中读不到，需 `bash -ic 'make dev'`）。
> `.env` 已被 `.gitignore` 忽略，密钥不入版本库；日志有 `SecretMaskingFilter` 脱敏。

---

## 常用命令

```bash
make test                                  # 全量测试（91 个用例）
make ask Q="刻蚀机腔体真空度异常怎么排查？"   # 命令行单问
make ask-stream Q="参数下限是多少"           # 流式链路
make clean                                 # 清理字节码与测试缓存
```

### 评测与验收（收尾任务测出来的，全部可复跑）

```bash
# 黄金集自检：章节引用是否真实存在、要点能否在引用章节命中
.venv/bin/python scripts/check_golden.py

# 文字类评测（45 条，产出 data/eval/report_*.json）
.venv/bin/python scripts/eval.py --badcases

# 图片类评测（5 条，需要先有 fixture）
.venv/bin/python scripts/make_image_fixtures.py     # 生成 5 张图（含 1 张故意模糊的负样本）
.venv/bin/python scripts/eval_image.py

# 演示场景走查（需先 make dev）：A 抢修 / B 培训 / C 参数+备件 / D 拒答
.venv/bin/python scripts/demo_walkthrough.py

# 并发压测（需先 make dev）
.venv/bin/python scripts/load_test.py -n 20
```

> `scripts/make_image_fixtures.py` 需要中文字体（脚本会自动探测 WSL 的 `/mnt/c/Windows/Fonts`、
> Noto CJK、macOS 字体等），找不到会**直接报错退出**而不是渲染出方块字。

---

## 目录结构

```
backend/
├── main.py            # FastAPI 入口 + lifespan 预热
├── setting.py         # 全部配置（调参不改代码）
├── schemas.py         # 请求/响应契约 + ServiceError
├── dependency.py      # 启动预热与进程级单例
├── db.py              # 业务库模型（当前配置 MySQL；SQLite 可一键切回）
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
| **启动预热** | `lifespan` 预加载 embedding 与精排（2026-09-18 复测约 **19.0s**）。不预热会把这段时间算进第一个请求的首 Token，顶穿 NFR-01（首 Token ≤ 2s） |
| **降级不中断** | 模型/MCP/数据库任一不可用都不阻止启动，由 `GET /api/health` 如实报 `degraded` 并给出原因 |
| **SSE 事件** | `meta → image? → token* → citations → done`，失败发 `error`；契约见 `接口文档.md` §6 |
| **token 缓冲** | token 在 `verify` 定稿后才发：verify 拒答时会改写答案，若已流式发出，用户会先看到操作步骤再看到拒答 |
| **引用归属** | LLM 只「尽量」逐句挂引用（实测 78%~94%）。`citation.enforce_citations()` 在出字前把漏引用的句子按文本重合度归属到最匹配的上下文块，**归属不上的句子直接省略** —— 于是「引用覆盖率 100%」是代码保证的，而不是靠拒答维持 |
| **图片即证据** | 识别结果会作为一个 `source_type=image` 的证据块注入上下文（参数表/铭牌的答案就在图里），并在引用卡片显示原图；不注入的话 LLM 只能回「材料里没有这张表」 |
| **多轮追问补全** | 追问（含指代词「它/刚才/上述」或过短）会把**最近一个自足提问**补进检索式；连续追问会回溯跳过中间的指代句 |
| **无引用即拒答** | 有材料却一句引用都没挂的答案判未覆盖（fail-closed）；伪造引用编号不被静默修复，一律拒答 |
| **零幻觉** | 引用由代码分配、verify 独立校验、证据不足即拒答；备件替代无依据时明确「需原厂确认」 |
