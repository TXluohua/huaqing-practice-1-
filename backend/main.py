"""FastAPI 入口（开发文档 6.2 的 main.py：入口 + lifespan 预热）。

启动方式
--------
    .venv/bin/python -m uvicorn backend.main:app --reload --port 8000
    或 make dev

挂载内容
--------
    /api/**            路由层（backend/routers）
    /static/uploads/** 上传的图片（引用卡片与缩略图直接 <img src> 取用）
    /docs              调试模式下开放 Swagger

依赖方向（开发文档 6.1）：main -> routers -> services -> agents/tools/rag -> config。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .dependency import shutdown, warm_up
from .routers import api_router, register_error_handlers
from .setting import get_settings

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """启动预热 / 关闭释放。"""

    settings = get_settings()
    # 日志级别与 debug 解耦：默认 INFO（NFR-03）。DEBUG 会让 LangGraph 把整个
    # 检查点状态打进日志，需要时显式设 LOG_LEVEL=DEBUG。
    from .utils.text import setup_logging

    effective_level = setup_logging(settings.log_level)
    logger.info(
        "启动 %s v%s（log_level=%s, debug=%s）",
        settings.app_name,
        settings.app_version,
        effective_level,
        settings.debug,
    )

    report = await warm_up()
    if not report.ok:
        # 不阻止启动：健康检查会如实报 degraded（对齐开发文档 R7 的降级原则）
        logger.warning("服务以降级状态启动，详见 GET %s/health", settings.api_prefix)

    try:
        yield
    finally:
        await shutdown()
        logger.info("服务已停止")


def create_app() -> FastAPI:
    """构造应用（便于测试用 TestClient 复用）。"""

    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description="半导体设备维护知识库智能问答系统 —— 后端接口",
        lifespan=lifespan,
        docs_url=settings.docs_url,
        redoc_url=None,
    )

    # 前端开发服务器（Vite，5173）跨域访问
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        # SSE 需要的自定义头
        expose_headers=["X-Trace-Id"],
    )

    # 统一错误体（接口文档 §5）
    register_error_handlers(app)

    # 业务路由：chat（问答/会话/上传/反馈/健康）+ admin（入库/索引/切片）
    app.include_router(api_router, prefix=settings.api_prefix)

    # 上传图片的静态访问：引用卡片与原图预览走这里
    upload_dir = Path(settings.upload_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)
    app.mount(
        f"{settings.static_url_prefix}/uploads",
        StaticFiles(directory=str(upload_dir)),
        name="uploads",
    )

    return app


#: uvicorn 的 ASGI 入口
app = create_app()
