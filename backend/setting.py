"""全局配置（开发文档 6.2 / 6.3：全部可调参数与密钥）。

约定（开发文档 6.4）：
    - 提示词、阈值、权重一律写入本文件与 .env，禁止硬编码在业务逻辑中；
    - 密钥只进 .env，不入版本库；日志与前端不得出现密钥。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

#: backend/ 目录
BACKEND_DIR = Path(__file__).resolve().parent
#: 项目根目录
PROJECT_ROOT = BACKEND_DIR.parent
#: 运行时数据目录（开发文档 6.2：backend/data，不入 git）
DATA_DIR = BACKEND_DIR / "data"


class Settings(BaseSettings):
    """全部配置项。环境变量名与本文件字段名一致（大小写不敏感）。"""

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------------------------------------------------------------- 应用
    app_name: str = "半导体设备维护知识库智能问答系统"
    app_version: str = "0.1.0"
    debug: bool = True
    #: 日志级别（NFR-03）。**与 debug 解耦**：debug 只控制 Swagger 是否开放，
    #: 日志级别单独配置。默认 INFO —— 设成 DEBUG 时 LangGraph 会把整个检查点
    #: 状态（含大段二进制 repr）打进日志，单次问答上万行，日常不要开。
    log_level: str = "INFO"
    api_prefix: str = "/api"
    #: 前端开发服务器（Vite）跨域白名单
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]

    # ------------------------------------------------------------ 目录与存储
    data_dir: Path = DATA_DIR
    upload_dir: Path = DATA_DIR / "uploads"
    image_dir: Path = DATA_DIR / "images"
    chroma_dir: Path = DATA_DIR / "chroma"
    sqlite_dir: Path = DATA_DIR / "sqlite"
    #: 会话检查点（backend/memory.py 使用）
    checkpoint_path: Path = DATA_DIR / "sqlite" / "checkpoints.sqlite"
    #: badcase 记录文件（FR-07：驳回案例记入）
    badcases_path: Path = PROJECT_ROOT / "data" / "eval" / "badcases.md"
    #: 静态资源访问前缀（引用卡片展示原图用）
    static_url_prefix: str = "/static"

    # ---------------------------------------------------------------- 业务库
    #: 开发期默认 SQLite，零依赖即可跑通全部接口；
    #: 生产按开发文档 5.3 改为 MySQL（同一套 SQLAlchemy 模型，只换 URL）：
    #:   DATABASE_URL=mysql+aiomysql://user:pass@127.0.0.1:3306/smka?charset=utf8mb4
    database_url: str = f"sqlite+aiosqlite:///{DATA_DIR / 'sqlite' / 'business.sqlite'}"
    db_echo: bool = False

    # ---------------------------------------------------------- 图片与多模态
    image_max_mb: float = 5.0
    image_min_side: int = 200
    #: 超过此边长则等比压缩（开发文档 5.2 FR-02：预处理做压缩与格式统一）
    image_max_side: int = 1600
    image_max_count: int = 3
    image_allowed_mime: list[str] = ["image/jpeg", "image/png", "image/webp"]
    image_jpeg_quality: int = 85

    # ---------------------------------------------------------------- 问答
    question_max_len: int = 2000
    answer_timeout_s: float = 60.0
    #: SSE 心跳间隔（秒），0 表示不发心跳
    sse_heartbeat_s: float = 15.0

    # ---------------------------------------------------------------- 模型
    deepseek_api_key: str = ""
    #: DeepSeek 接口地址（追加字段：开发文档附录 C 的 DEEPSEEK_BASE_URL）
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"
    dashscope_api_key: str = ""
    vlm_model: str = "qwen-vl-max"

    # ---------------------------------------------------- RAG 参数（供 A 轨使用）
    # 本层不直接消费，集中在此以便「调参不改代码」（NFR-04）
    chunk_size: int = 800
    chunk_overlap: int = 120
    retrieve_top_k: int = 40
    rerank_top_n: int = 8
    context_token_budget: int = 6000
    #: 置信度三级阈值（FR-05：>=0.80 高 / 0.60-0.80 中 / <0.60 低）
    confidence_high: float = 0.80
    confidence_medium: float = 0.60
    #: 精排最高分低于该值即拒答
    reject_score_threshold: float = 0.0

    # ------------------------------------------------ RAG 检索与向量化（追加）
    # 2026-09 追加（RAG 侧接入需要；全部为新增字段，未改动上面任何既有字段）。
    #: embedding 提供方：local（默认，本地 HF 模型）/ auto / ollama / hashing / dashscope
    #:
    #: **默认 local：向量化完全在本地跑，不调用任何外部 API。**
    #: - local  ：本地 HuggingFace 模型（embedding_model），模型缺失即报错并提示
    #:            运行 scripts/prepare_model.py（不静默降级、不偷偷调云端接口）
    #: - auto   ：local → ollama → hashing 的降级链（**不含云端接口**）
    #: - dashscope：仅在你显式配置时才用阿里云接口（属云端调用，默认不用）
    embedding_provider: str = "local"
    #: 本地向量化模型：HuggingFace 仓库名，或**绝对路径**（指向已部署的模型目录）
    embedding_model: str = "BAAI/bge-large-zh-v1.5"
    #: 只允许用本地模型、不发起下载（无外网环境必须为 True；默认即纯本地）
    embedding_offline: bool = True
    #: 本地模型目录：HF_HOME 指向此处 —— 模型落在项目内，不写用户家目录。
    #: 部署（Docker / 换机）只需同步这一个目录，或在构建阶段下载一次。
    model_dir: Path = DATA_DIR / "models"
    #: 是否允许联网下载模型（仅 scripts/prepare_model.py --download 时临时打开）
    model_allow_download: bool = False
    #: HuggingFace 端点（留空则沿用环境变量 HF_ENDPOINT；国内常用 https://hf-mirror.com）
    hf_endpoint: str = ""
    #: Ollama 本地服务（开发文档 5.3 的 bge-m3 路线）
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_embedding_model: str = "bge-m3"
    #: Ollama 调用超时（秒）。原先写死 10s，导致 32 条切片批量向量化必 ReadTimeout，
    #: 实际把「bge-m3 路线」堵死了（甲侧实测缺陷 #8）。改为可配，默认 60s。
    ollama_timeout_s: float = 60.0
    #: Ollama 单次请求的文本条数（分批发送，避免一次请求过大而超时）
    ollama_batch_size: int = 16
    #: 阿里云向量化模型（配置 DASHSCOPE_API_KEY 后可用）
    dashscope_embedding_model: str = "text-embedding-v3"
    #: 向量库后端：auto / chroma / simple（simple 为内置零依赖兜底实现）
    vector_backend: str = "auto"
    #: 精排提供方：auto / cross-encoder / lexical
    rerank_provider: str = "auto"
    #: 精排模型：仓库名或项目内模型目录路径（默认 backend/data/models/bge-reranker-v2-m3）
    rerank_model: str = "BAAI/bge-reranker-v2-m3"
    #: 送入精排的候选上限（按融合分取前 N）。CPU 实测：bge-reranker-v2-m3 每对约 0.3~0.5s，
    #: 32 对要 30s+ —— 会直接顶穿 P95 ≤ 8s，故必须限流。有 GPU 时可调到 20~30 提升召回。
    rerank_max_candidates: int = 12
    #: 精排输入的最大 token 数（截断）。256 相比默认 512 约省一半时间，
    #: 对「查询-切片相关性」判断影响很小（关键信息通常在切片开头）。
    rerank_max_length: int = 256
    #: 精排批大小
    rerank_batch_size: int = 8
    #: 混合检索权重（BM25 词法 / 向量语义），RRF 融合用
    bm25_weight: float = 0.4
    vector_weight: float = 0.6
    #: RRF 平滑常数（越大越平缓）
    rrf_k: int = 60
    #: 「证据是否充分」判定阈值（精排**绝对分**，0~1；不是 min-max 相对分）
    #: Cross-Encoder 路径用本值（sigmoid 概率，0.5 近似相关/不相关分界）
    sufficient_score_threshold: float = 0.5
    #: lexical 降级精排的阈值：lexical 分数量纲偏低（实测正样本 0.30~0.70），
    #: 故只作「明显不相关」的软下限，负样本主要靠 anchor_check 的型号/术语规则拦。
    #: 换回 Cross-Encoder 后应使用 sufficient_score_threshold。
    sufficient_score_threshold_lexical: float = 0.25
    #: 问题关键术语在检索材料中的覆盖率下限（低于即拒答，见 citation.anchor_check）
    anchor_term_coverage: float = 0.5
    #: 问题关键术语在**整个知识库**中缺失的比例上限（超出即判「知识库未覆盖」）
    #: 依据 20 条黄金集实测：正样本最大 0.33、负样本 0.43，取 0.40 留双向余量；
    #: 黄金集扩到 50 条后必须重新校准。
    anchor_kb_missing_ratio: float = 0.4
    #: 引用归属阈值：LLM 答案里未挂引用的结论句，程序化归属到上下文块时的
    #: 最小「句子-块」字符二元组重合度。低于该值视为无法归属 → 该句不进入答案
    #: （宁可少说，也不给没有依据的句子）
    citation_attribution_threshold: float = 0.45
    #: 引用来源权威度权重（置信度公式的 0.25 项，键为 category）
    authority_weights: dict[str, float] = {
        "标准": 1.0,
        "设备维护": 0.9,
        "工艺": 0.85,
        "ticket": 0.6,
        "image": 0.5,
    }

    # ------------------------------------ 受限 agent 与多模态（平台侧甲 追加）
    # 2026-09 追加（augment / ingest_image 需要；全部为**新增字段**，
    # 未改动上面任何既有字段，符合协作规范「只追加」约定）。
    #: augment 硬边界（开发文档 4.3）：轮数 / 工具调用次数 / 超时（秒）
    #: 由代码强制，不交给模型自觉 —— 调参改这里，不改代码（NFR-04）。
    augment_max_rounds: int = 2
    augment_max_tool_calls: int = 3
    augment_timeout_s: float = 15.0
    #: augment 允许调用的工具（与 tools/__init__.py 注册表取交集后生效）
    #: 三个补检索工具 + 工单检索（MCP，FR-03 跨源）+ 备件查询（FR-10）
    augment_tool_whitelist: list[str] = [
        "kb_search_expand",
        "kb_get_chunk",
        "kb_stats",
        "ticket_search",
        "parts_query",
    ]
    #: 触发备件查询的问题关键词（FR-10）：命中即调用 parts_query，结果作为证据进上下文
    parts_question_keywords: list[str] = [
        "库存",
        "备件",
        "配件",
        "替代",
        "替换件",
        "到货",
        "交期",
        "缺货",
        "有货",
        "订货",
        "采购",
        "询价",
        "料号",
        "sp-eta",
    ]
    #: 触发工单检索的问题关键词（FR-03 跨源）：命中即查历史工单案例
    ticket_question_keywords: list[str] = [
        "工单",
        "案例",
        "以前",
        "历史",
        "处置",
        "怎么处理",
        "发生过",
        "经验",
        "类似",
    ]
    #: 图片识别最低置信度：低于此值视为「未能识别」，提示改用文字描述（FR-02 失败处理）
    vision_min_confidence: float = 0.5
    #: 单张图片 VLM 调用超时（秒）
    vision_timeout_s: float = 30.0
    #: 备件示例数据源（FR-10）。接真实 ERP/采购系统时替换该文件即可。
    parts_inventory_path: Path = BACKEND_DIR / "config" / "parts_inventory.yaml"
    #: 备件库存在低于该值时提示「库存紧张」
    parts_low_stock: int = 5

    # ---------------------------------------------------------------- MCP
    mcp_ticket_server: str = str(BACKEND_DIR / "mcp_servers" / "ticket_server.py")
    mcp_enabled: bool = True
    #: MCP 工具加载超时（秒）：server 起不来时必须能及时放弃，不能卡住应用启动
    mcp_load_timeout_s: float = 20.0
    #: 工单数据源（FR-03 跨源检索的「工单源」）。接真实工单系统时替换该文件即可。
    tickets_path: Path = BACKEND_DIR / "config" / "tickets.yaml"
    #: 高频问题统计：低于该次数不进 Top 榜（FR-09）
    frequent_question_min_count: int = 1

    # ---------------------------------------------------------------- 方法
    def ensure_dirs(self) -> None:
        """创建全部运行时目录（应用启动时调用）。"""

        for path in (
            self.data_dir,
            self.upload_dir,
            self.image_dir,
            self.chroma_dir,
            self.sqlite_dir,
            self.checkpoint_path.parent,
        ):
            Path(path).mkdir(parents=True, exist_ok=True)

    @property
    def docs_url(self) -> str | None:
        """关闭调试时隐藏 Swagger，避免生产暴露接口清单。"""

        return "/docs" if self.debug else None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """取全局配置单例。"""

    return Settings()


#: 便捷单例（仅只读使用）
settings = get_settings()
