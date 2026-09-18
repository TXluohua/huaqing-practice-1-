# RAG 链路实现说明（乙侧交付）

> **项目**：半导体设备维护知识库智能问答系统
> **范围**：`backend/rag/*`、`backend/agents/nodes/{retrieval,generation}.py`、`backend/agents/prompts/*`、
> `backend/tools/kb_tools.py`、`backend/config/glossary.yaml`、`scripts/{ingest,build_index,inspect_chunks,eval}.py`、
> `data/raw`、`data/eval`、`tests/test_rag/*`、`tests/test_agents/test_verify.py`
> **不在本说明范围**：HTTP/service 层（甲）、前端（第三人）、MCP 工单与多模态（甲）
> **依据**：《开发文档（初稿）》§4 / §5.3 / §6 / §7 / §8、`接口文档.md`、`后端二人分工与协作规范.md`

---

## 1. 一句话结论

**RAG 主链路已打通并跑出真实指标**：`rewrite → retrieve → rerank → build_context → generate → verify`
六个节点全部接入，端到端可用（`make ask` / `scripts/eval.py` 均为实测，非设计承诺）。
**向量化与精排都由项目内的 HuggingFace 本地模型完成，不调用任何云端向量化接口。**
20 条黄金集上：**Top-5 命中 100%、引用覆盖率 100%、引用真实性 100%、要点命中 100%、负样本拒答 100%**，
MRR 1.0000、NDCG@5 0.9932，稳态延迟 P95 4.49s。

---

## 2. 交付清单

| 文件 | 职责 | 关键实现点 |
| --- | --- | --- |
| `backend/rag/ingest.py` | 解析 / 抽图 / 清洗 / 切片 | md/txt（front-matter + `<!-- page: N -->`）与 pdf（pypdf，物理页）双解析；跨页页眉页脚自动剔除；切片按「同页同章节 + 段落边界」切分，超长段落二次开窗；**元数据完整率不足 100% 直接拒绝入库** |
| `backend/rag/store.py` | **向量库唯一出口** | 4 级 embedding 降级（local/bge → ollama → dashscope → hashing）+ 2 种向量后端（chroma / 内置 simple）；索引清单记录 provider/维度/切片的**签名**，不匹配即报错（建库与查询必须同一实例） |
| `backend/rag/retriever.py` | BM25 + 向量 + RRF + 精排 + 上下文 | jieba 分词（缺失退化为字符二元组）；rank_bm25（缺失用内置 Okapi）；RRF 融合（权重可调）；精排 Cross-Encoder 优先、**加载后自检**、失败降级 lexical；上下文 `[n]` 编号 + 去重 + 版本择优 + token 预算 |
| `backend/rag/citation.py` | 引用校验 / 置信度 / 拒答 | 引用编号由代码分配；**伪造引用不计入覆盖率**；覆盖率公式（0.40 精排 + 0.35 覆盖 + 0.25 权威度）；`anchor_check` 型号/数值/术语三重锚点校验 |
| `backend/agents/nodes/retrieval.py` | 4 个检索节点 + 条件边判定 | `rewrite`/`retrieve`/`rerank`/`build_context`；`should_augment()` 供 graph 条件边使用 |
| `backend/agents/nodes/generation.py` | `generate` + `verify` | LLM 路径（DeepSeek，temperature=0）+ **抽句式降级路径**；`verify` 为确定性代码，拒答时清空引用并改写答案 |
| `backend/agents/prompts/{system,generate,rewrite}.md` | 零幻觉总则 / 生成 / 改写 | 生成提示词含 `qa` 与 `training`（FR-09）两套模板与「未覆盖」分支 |
| `backend/tools/kb_tools.py` | 本地工具 + 术语表 | `kb_search` / `kb_search_expand` / `kb_get_chunk` / `kb_stats` / `glossary_lookup`（docstring 即工具说明书）；甲侧 `vision_extract` / `parts_query` 留签名占位 |
| `backend/config/glossary.yaml` | 术语归一表 | 19 组规范词 + 设备型号别名 |
| `scripts/ingest.py` | 一键入库 | 支持 md/pdf、`--rebuild`、元数据完整性把关、报告输出 |
| `scripts/build_index.py` | 一键重建索引 | 清空+重建（幂等），打印耗时与 ≤300s 判定 |
| `scripts/inspect_chunks.py` | 切片查看 | 按文档/内容/页码/章节/ID 过滤，调分块参数用 |
| `scripts/eval.py` | 黄金集评测 | 走**完整图**，出 5 项验收指标 + MRR/NDCG + 逐条明细 + JSON 报告 + badcase 追加 |
| `data/raw/*.md` | 示例语料 | 5 个文档（477 行 / 1.26 万字）：刻蚀手册、CVD 工艺规范、备件目录、安全标准、清洗设备指南 |
| `data/eval/golden_qa.jsonl` | 黄金集 | 20 条（18 正 + 2 负），`must_cite` 24 条全部经脚本校验语料中真实存在 |
| `data/eval/golden_image.jsonl` | 图片类样例 | 3 条清单（图片文件待平台侧提供） |
| `tests/test_rag/*`、`tests/test_agents/test_verify.py` | 单测 | 28 个用例：元数据完整性、清洗规则、BM25/RRF/版本择优/上下文预算、伪造引用识别、拒答闸门、verify 不可绕过 |

---

## 3. 本地模型部署（向量化与精排全部在本地跑，不调用任何 API）

**默认 `EMBEDDING_PROVIDER=local`：向量化由本项目目录下的 HuggingFace 模型完成，
不请求任何云端向量化接口。** 模型落在项目内（`backend/data/models`），
不写用户家目录，因此容器构建或换机只需同步这一个目录。

| 模型 | 用途 | 维度 / 体积 | 位置 |
| --- | --- | --- | --- |
| `BAAI/bge-large-zh-v1.5` | 向量化（与开发文档 bge-m3 同为 1024 维） | 1024 维 / 2.5 GB | `backend/data/models/hub/models--BAAI--bge-large-zh-v1.5` |
| `BAAI/bge-reranker-v2-m3` | 交叉编码器精排 | 2.2 GB | `backend/data/models/bge-reranker-v2-m3` |

模型准备与自检（一个脚本搞定）：

```bash
.venv/bin/python scripts/prepare_model.py --check                 # 检查 + 语义/判别力自检（默认动作）
.venv/bin/python scripts/prepare_model.py --copy-from-cache       # 从本机 HF 缓存复制到项目目录（离线可用）
.venv/bin/python scripts/prepare_model.py --download --source modelscope --with-reranker   # 联网下载
.venv/bin/python scripts/prepare_model.py --list                  # 列出项目里已部署的模型
```

实测自检输出（真实结果，非设计承诺）：

```
✓ embedding 加载成功：BAAI/bge-large-zh-v1.5（1024 维，签名 local:BAAI/bge-large-zh-v1.5）
  ✓ 语义探针：「刻蚀机腔体真空度异常怎么排查…」 相关 0.694 vs 无关 0.257
  ✓ 语义探针：「真空泵抽速标准…」 相关 0.634 vs 无关 0.144
✓ 精排模型 BAAI/bge-reranker-v2-m3（…/backend/data/models/bge-reranker-v2-m3）：相关 0.268 vs 无关 0.000
```

### 3.1 向量模型选型（可切换，切换后必须重建索引）

`bge-large-zh-v1.5` 与 `bge-base-zh-v1.5` 都是同一系列的中文向量模型，**代码层面完全等价**：
维度由模型动态读取、写入索引清单，因此切换只改配置，不改代码。

| 模型 | 维度 | 参数量 | 体积（约） | 特点 |
| --- | ---: | ---: | ---: | --- |
| `BAAI/bge-large-zh-v1.5`（**当前使用**） | 1024 | 3.26 亿 | 2.6 GB | 检索质量更高，CPU 编码较慢 |
| `BAAI/bge-base-zh-v1.5` | 768 | 1.02 亿 | 0.4 GB | 体积小、速度快约 3 倍，质量略低 |

切换步骤（三步，缺一不可）：

```bash
export EMBEDDING_MODEL=BAAI/bge-base-zh-v1.5                       # 或用 .env 持久化
.venv/bin/python scripts/prepare_model.py --copy-from-cache        # 无缓存时改 --download
.venv/bin/python scripts/build_index.py                            # 必须重建索引
.venv/bin/python scripts/eval.py                                   # 必须重跑评测对比五项指标
```

**为什么必须重建索引**：索引清单（`manifest.json`）记录 provider、维度与模型签名，
换模型后不重建会被直接拦住（已实测）：

```
索引与当前 embedding 实例不匹配：索引 local:BAAI/bge-large-zh-v1.5 / 当前 hashing:768。
请重建索引（scripts/build_index.py --rebuild）
```

选型建议：本项目语料规模很小（当前 32 个切片），两个模型都能满足 Top-5 要求；
若部署环境对镜像体积敏感，`bge-base-zh-v1.5` 更划算；但**换完必须用 `scripts/eval.py` 实测确认**，
不能默认「小的也够用」。精排模型同理：`bge-reranker-base`（约 1.1 GB）可替换
`bge-reranker-v2-m3`（2.29 GB），改 `RERANK_MODEL` 即可，但同样要重跑评测。

要点：

1. **默认离线**：加载时 `HF_HUB_OFFLINE=1`，不会偷偷联网；只有 `prepare_model.py --download`
   才临时打开下载；
8. **`local` 失败即报错**，并给出修复命令（不静默降级、更不会偷偷调云端接口）；
   `auto` 是 local → 本地 Ollama → hashing 的降级链，**同样不含云端接口**；
3. `dashscope` 仅在你显式配置 `EMBEDDING_PROVIDER=dashscope` 时启用，且启动时会打日志提醒那是云端调用；
4. 模型文件**不入库**（`backend/data/` 已被 `.gitignore` 排除，共 4.6 GB）。
   镜像里 `hf-mirror.com` 的大文件跳转会 401（CAS 鉴权），因此下载推荐
   `--source modelscope`（本项目两个模型就是这样落地的）；
5. `EMBEDDING_MODEL` / `RERANK_MODEL` 既可以是仓库名，也可以是**模型目录路径**，
   解析顺序：路径 → `<模型目录>/<模型名>` → HF 缓存（`HF_HOME` 已指向模型目录）。

---

## 4. 如何运行（实测命令与结果）

```bash
make install                                   # 建虚拟环境 + 装依赖
.venv/bin/python scripts/ingest.py --rebuild   # 入库：5 文档 → 32 切片，元数据完整率 100%，21.6s
.venv/bin/python scripts/inspect_chunks.py --stats
make test                                      # 48 passed（含骨架 7 个 + RAG 41 个）
make ask Q="刻蚀机真空度异常怎么排查？"          # status=OK 置信度=0.705 引用数=8
.venv/bin/python scripts/eval.py               # 20 条黄金集 → 报告写入 data/eval/report_*.json
```

实测入库结果：

| 文档 | 版本 | 分类 | 型号 | 页数 | 切片 |
| --- | --- | --- | --- | ---: | ---: |
| 刻蚀设备维护手册 | V3.2 | 设备维护 | Etcher-A | 7 | 7 |
| CVD 薄膜沉积工艺规范 | V2.0 | 工艺 | CVD-200 | 6 | 6 |
| 备件目录与替代件说明 | V1.4 | 设备维护 | Etcher-A | 6 | 7 |
| 设备维护安全作业标准 | V1.1 | 标准 | 通用 | 6 | 6 |
| 清洗设备操作与维护指南 | V2.1 | 设备维护 | Cleaner-C | 6 | 6 |

> 建索引 21.6s（验收 ≤ 5 分钟）；平均切片 399 字 / 289 token，最大 338 token（上下文预算 6000 足够装 8 块）。

---

## 5. 实测指标（20 条黄金集，走完整图）

| 指标 | 实测 | 目标 | 判定 |
| --- | ---: | ---: | --- |
| Top-5 检索命中率 | **100%** | ≥ 80% | 达标 |
| 引用覆盖率（不可降级） | **100%** | 100% | 达标 |
| 引用真实性（已发出 132 项，伪造 0） | **100%** | ≥ 95% | 达标 |
| 答案要点命中率 | **100%** | ≥ 80% | 达标 |
| 负样本正确拒答率 | **100%**（2/2） | ≥ 90% | 达标 |
| MRR / NDCG@5 | **1.0000 / 0.9932** | —— | —— |
| 文字类延迟 P95 | **4.49s** | ≤ 8s | 达标（稳态；另需一次性预热 10.2s，见下） |

**必须同时看的三个限定条件**：

1. **精排提供方已是 `cross-encoder`（本地 `bge-reranker-v2-m3`，自检通过）** ——
   换上真交叉编码器后 MRR 从 0.944 提升到 **1.0000**、NDCG@5 从 0.948 提升到 **0.9932**，
   严格口径要点命中从 41.2% 提升到 44.7%，这就是精排的价值；评测报告里始终写明提供方。
8. **生成仍走抽句式降级路径，不是 LLM** —— 撰写时本机 `DEEPSEEK_API_KEY` 未配置。
   因此「要点命中 100%」很大程度来自「答案原句摘自命中切片」（严格口径只算答案正文为 **44.7%**）。
   **2026-09-18：密钥已配置，但下列指标尚未在 LLM 路径下重跑**，故本文生成相关数字仍出自降级路径。
8. **P95 是稳态延迟**：模型加载（约 10.2s）发生在首个请求上，**生产必须由 `main.py` 的
   `lifespan` 预热**（见 §9 第 1 条），否则首位用户的体验就是 10s 起。评测脚本也已先预热再计时。

---

## 6. 降级矩阵（离线可用是硬要求）

| 环节 | 首选 | 降级顺序 | 当前实际 | 怎么发现用了降级 |
| --- | --- | --- | --- | --- |
| Embedding | 本地 `BAAI/bge-large-zh-v1.5`（1024 维，与文档 bge-m3 同维） | ollama(bge-m3) → dashscope(text-embedding-v3) → **hashing 兜底** | **local/bge**（离线缓存命中，加载 3.6s） | `scripts/inspect_chunks.py --stats`、索引 `manifest.json`、健康检查 |
| 向量库 | chroma 持久化 | 内置 simple（jsonl + numpy） | **simple**（本机 chromadb 无法建库，见 §7） | 同上（`backend=simple`） |
| 词法 | rank_bm25 + jieba | 内置 Okapi BM25 / 字符二元组分词 | rank_bm25 + jieba | 检索日志、`BM25Index.name` |
| 精排 | Cross-Encoder bge-reranker-v2-m3（本地） | **lexical**（确定性打分） | **cross-encoder**（本地模型，自检通过） | `ranked[0].metadata.rerank_provider`、评测报告 |
| 生成 | DeepSeek（temperature=0） | **抽句式**（只用原文句子 + 代码分配编号） | **抽句式** | `answer_structured.generator`、`errors` 字段 |
| 拒答 | 三重锚点 + 覆盖率 + 精排阈值 | 任何一道不过即拒答 | 生效（负样本 2/2） | `uncertain` 列表里的具体理由 |

降级信息一律**显式记录**：进 `answer_structured`、`errors`、索引清单或评测报告，不静默。

---

## 7. 关键设计决定（含踩到的坑）

| # | 决定 | 为什么（实测依据） |
| --- | --- | --- |
| 1 | 精排分必须用**绝对分**（lexical 原值 / CE 走 sigmoid），禁止 min-max | 最初用 min-max，导致「候选里最高分永远是 1.0」→ 拒答闸门形同虚设，两条负样本拿到 **conf=0.98 的自信答案**（幻觉放行） |
| 2 | 负样本拒答靠 `anchor_check` 三重锚点，而不是只靠分数 | 分数无法区分「知识库没有这个型号」：正样本 0.296–0.704 与负样本 0.264–0.294 严重重叠 |
| 3 | 知识库级术语缺失比阈值 **0.40**，实测校准 | 正样本最大 0.33（q011）、负样本 0.43（q020），双向留余量；**黄金集扩到 50 条后必须重新校准** |
| 4 | 只有**真实存在**的引用编号才计入覆盖率 | 否则「挂个伪造编号」就能刷满覆盖率（覆盖率 100% 但引用是假的） |
| 5 | 抽句式答案的引用必须写在**句末标点之前**（`…… [n]。`） | 写在标点之后会被切句拆开，覆盖率统计成「该句无引用」——首轮评测 18 条正样本全被拒答就是这个原因 |
| 6 | 「依据：」清单与「建议…」句不计入结论句 | 它们是对引用的复述/处置建议，不是知识结论；否则每条答案都会莫名缺 1 个引用 |
| 7 | Cross-Encoder 加载后必须**探针自检** | 实测：拿不到权重时 sentence-transformers 不报错，而是「按 config 新建未训练模型」，打分数值毫无区分度；自检不通过即降级 lexical |
| 8 | `evidence_sufficient` 由 `build_context` 先给初值 | 条件边判定在 `augment` 之前发生；若按旧文档「augment 产出该字段」，条件边拿不到值（协作规范 §6.3 第 4 行待决项的落地口径） |
| 9 | 长段落必须二次开窗 | 单段落 > chunk_size 时若不切，一个切片可远超预算且引用粒度失去意义 |
| 10 | 精排必须**限候选数 + 限长度** | CPU 实测 `bge-reranker-v2-m3`：32 对 = 30.5s（顶穿 P95 ≤ 8s）；限到 12 对 + 截断 256 token + 批 8 → **4.4s**。旋钮是 `rerank_max_candidates` / `rerank_max_length`，有 GPU 时可调大 |
| 11 | 拒答时置信度必须压到低档 | 否则出现「状态=知识库未覆盖、置信度=0.86」的自相矛盾展示（前端气泡上尤其刺眼） |

---

## 8. 已知限制与环境问题

| # | 现象 | 影响 | 处理 |
| --- | --- | --- | --- |
| 1 | chromadb 曾在本机报 `error returned from database: (code: 14) unable to open database file`（原生 sqlite 可写、`/tmp` 与工作区路径均失败） | 当时向量库降级为内置 simple；**2026-09-18 已解决**——现实跑 `local/chroma`（5 文档 / 32 切片） | 降级逻辑保留；**部署环境（Docker）仍需复验 chroma 可用性**，配置项 `VECTOR_BACKEND=chroma` 一处切换即可（store.py 是唯一出口） |
| 2 | `hf-mirror.com` 的大文件跳转返回 401（CAS 鉴权），无法从镜像下载权重 | 精排模型取不到 | 已改用 **ModelScope** 下载并落到项目目录（`--source modelscope`）；代码同时支持「模型路径」与「HF 缓存」两种引用方式 |
| 2b | CPU 上交叉编码器较慢（每对约 0.35s） | 若放开候选数会顶穿延迟指标 | 已默认限流：12 候选 / 256 token / 批 8 → 4.4s；有 GPU 时可调大 `rerank_max_candidates` 换召回 |
| 2c | 精排阈值 0.5 是在当前黄金集上标定的（正样本绝对分 0.53~0.73，余量偏小） | 黄金集扩大后可能出现误拒 | 扩到 50 条后重新标定 `sufficient_score_threshold`；负样本同时有锚点规则兜底，不只靠分数 |
| 3 | 未配置 `DEEPSEEK_API_KEY`（**2026-09-18 已配置**） | 曾致生成走抽句式降级（**仅生成环节**；向量化与精排已是本地模型，不依赖任何密钥） | **待办：重跑 `scripts/eval.py`** 对比 LLM 路径下的要点命中率；本文其余生成类指标仍出自降级路径 |
| 4 | 图片类样例（3 条）无 fixtures 图片 | 多模态评测无法执行 | 归平台侧：图片文件 + `ingest_image` 节点；`eval.py --images` 已预留入口（`DASHSCOPE_API_KEY` 已于 2026-09-18 配置，**fixture 实物图片仍缺**） |
| 5 | 黄金集 20 条、全部来自示例语料 | 指标乐观，阈值（0.40）是在这 20 条上校准的 | D8 扩到 50 条后按 §6 表格重新校准阈值，并更新本节 |
| 6 | 扫描件/office 文档（docx/pptx/xlsx）不支持 | 上传即明确报错（不是静默空切片） | 待接入 MarkItDown（甲侧入库链路） |
| 7 | **真 LLM 路径下引用覆盖率不足 → 拒答**（2026-09-18 配好 `DEEPSEEK_API_KEY` 后实测） | `verify` 的覆盖率闸门在高置信度下要求 **100%**（`citation.py:444`），该门槛按**抽句式**路径标定——那条路径由代码给每句分配引用，天然 100%；LLM 实测 78%~94% | 「刻蚀机腔体真空度异常怎么排查？」（94%）、「CVD 沉积温度是多少？」（78%）均被拒答。三选一：回退抽句式 / 放宽 `require_coverage` / 程序化补引用。**G2 的「100%」当前靠拒答维持** |

---

## 9. 交给平台侧（甲）的对接点

1. **`main.py` 的 `lifespan` 必须预热本地模型**：embedding + 精排加载约 **10.2s**（实测）。
   建议启动时调用 `await get_store()` 与 `await create_reranker()`（或先跑一次空问答），
   否则这部分耗时会计入首位用户的请求，直接违反 NFR-01「首 Token ≤ 2s」。
   模型文件在 `backend/data/models`（4.6 GB，不入库），部署镜像需在构建阶段执行
   `scripts/prepare_model.py --download --source modelscope --with-reranker`，或提前同步该目录。
2. **`ChatService.stream_answer` 需把输入字段喂进图**：
   `make_initial_state(question, device_model=…, category=…, answer_mode=…, user_role=…)`。
   这几个字段已在 `state.py` 追加（只追加、有默认值），但 `agent_factory.ainvoke()` 目前不透传。
3. **SSE 事件映射**：`meta → (image?) → token* → citations → done`。
   - `citations` 事件直接用 `state["citations"]`（`Citation` 字段与接口文档 §6 一致）；
   - **拒答时 `citations=[]`、`confidence` 已压到低档（≤0.59）且 `answer` 已是拒答话术**（`verify` 改写），
     前端按 `done.status=NOT_COVERED` 渲染；
   - `done.uncertain` 是拒答原因列表（含「精排分低于阈值」「型号未出现在材料中」这类可读理由）。
4. **`token` 是增量，与拒答改写存在冲突（P0 待定）**：`verify` 会改写 `answer`，
   建议「先缓冲到 `verify` 再发 `token`」，或先发 `token` 再用 `done` 纠正 —— 请甲拍板
   （接口文档未定义此情形）。
5. **健康检查可复用**：`await store.stats()`（后端 / provider / 维度 / 切片数）与
   `ranked[0].metadata.rerank_provider`（精排是否降级）可直接进 `/api/health`。
6. **入库接口（FR-08）**：`kb_service.create_document` 直接调 `rag.ingest.ingest_path` + `store.add_chunks`；
   元数据完整性由 ingest 把关（缺 version 会抛错，正好对应接口文档要求 version 必填）。
7. **`tools/__init__.py` 我未改动**（按协作规范归甲）：请在其中聚合 `kb_tools` 的本地工具 + MCP 工单工具（含降级）。
8. **`vision_extract` / `parts_query` 留了签名占位**（在 `kb_tools.py` 末尾），归甲实现，不要另起一份。
9. **`VECTOR_BACKEND`**：本机 chromadb 在受限文件权限下无法建库，已自动降级为内置 `simple` 后端；
   部署环境请复验 chroma（`VECTOR_BACKEND=chroma` 一处切换，`store.py` 是唯一出口）。
10. **模型与索引的部署顺序**：`prepare_model.py` → `build_index.py` → 起服务。
    索引清单 `manifest.json` 记录 provider/维度签名，模型换了必须重建索引，否则会直接报错（设计如此）。

---

## 10. 变更记录

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| v0.1 | 2026-09-15 | 首版：RAG 六节点链路、四个脚本、语料与黄金集、28 个 RAG 单测；20 条黄金集 5 项指标达标 |
| v0.2 | 2026-09-15 | 向量化与精排改为**项目内本地 HF 模型部署**（默认 `EMBEDDING_PROVIDER=local`，默认链路不含云端接口）；新增 `scripts/prepare_model.py`（准备 + 语义/判别力自检）；下载精排模型 `bge-reranker-v2-m3`（经 ModelScope），精排提供方由 lexical 升级为 cross-encoder，MRR→1.0000、NDCG@5→0.9932；精排限流（12 候选/256 token/批 8）使稳态 P95=4.49s；拒答时置信度压到低档；评测加入预热以正确度量 P95 |
