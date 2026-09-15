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

    # ---------------------------------------------------------------- MCP
    mcp_ticket_server: str = str(BACKEND_DIR / "mcp_servers" / "ticket_server.py")
    mcp_enabled: bool = True

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
