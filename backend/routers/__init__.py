"""路由层公共设施：错误类型、统一错误处理、service 依赖、路由聚合。

开发文档 6.1 规定依赖方向 routers -> services -> agents/tools/rag -> config，禁止反向 import。
因此本层**不写业务逻辑**，只做四件事：

    1. 参数校验（由 schemas.py 的 Pydantic 模型承担）
    2. 调用 service 层（经 Depends 注入，便于测试时 dependency_overrides 替换）
    3. 把结果包成 HTTP 响应 / SSE 流
    4. 把异常翻译成统一错误体（接口文档 §5）

main.py 只需两行即可挂载：

    from backend.routers import api_router, register_error_handlers
    register_error_handlers(app)
    app.include_router(api_router, prefix=settings.api_prefix)
"""

from __future__ import annotations

import importlib
import logging
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..agents.state import new_trace_id
from ..schemas import ErrorBody, ServiceError

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# 错误类型
# --------------------------------------------------------------------------- #


class ApiError(Exception):
    """带业务错误码的接口异常，由 register_error_handlers 统一渲染为 ErrorBody。

    错误码取值见接口文档 §5。
    """

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.detail = detail


#: HTTP 状态码 -> 默认错误码（service 未指定时使用）
_DEFAULT_CODES: dict[int, str] = {
    400: "INVALID_ARGUMENT",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    413: "PAYLOAD_TOO_LARGE",
    415: "UNSUPPORTED_MEDIA_TYPE",
    422: "INVALID_ARGUMENT",
    429: "RATE_LIMITED",
    500: "INTERNAL_ERROR",
    501: "NOT_IMPLEMENTED",
    503: "SERVICE_UNAVAILABLE",
}


def _error_response(
    status_code: int,
    code: str,
    message: str,
    detail: dict[str, Any] | None = None,
) -> JSONResponse:
    body = ErrorBody(code=code, message=message, detail=detail, trace_id=new_trace_id())
    return JSONResponse(status_code=status_code, content=body.model_dump(exclude_none=True))


def _serializable_errors(exc: RequestValidationError) -> list[dict[str, Any]]:
    """把 Pydantic 校验错误转成可 JSON 序列化的结构。

    坑：field_validator 里抛出的 ValueError 会原样出现在 exc.errors() 的 ctx.error 中，
    直接交给 JSONResponse 会抛 TypeError（Object of type ValueError is not JSON serializable），
    结果本该是 400 的响应变成 500。因此这里只保留可序列化字段，并把 ctx 值转成字符串。
    """

    serialized: list[dict[str, Any]] = []
    for err in exc.errors():
        item: dict[str, Any] = {
            "type": str(err.get("type", "value_error")),
            "loc": [str(part) for part in err.get("loc", ())],
            "msg": str(err.get("msg", "")),
        }
        ctx = err.get("ctx")
        if isinstance(ctx, dict) and ctx:
            item["ctx"] = {str(k): str(v) for k, v in ctx.items()}
        serialized.append(item)
    return serialized


def register_error_handlers(app: FastAPI) -> None:
    """注册统一错误处理，保证任何失败都返回接口文档 §5 的错误体。"""

    @app.exception_handler(ApiError)
    async def _handle_api_error(_: Request, exc: ApiError) -> JSONResponse:
        return _error_response(exc.status_code, exc.code, exc.message, exc.detail)

    @app.exception_handler(ServiceError)
    async def _handle_service_error(_: Request, exc: ServiceError) -> JSONResponse:
        # service 层用 ServiceError 表达业务错误（413 IMAGE_TOO_LARGE / 404 QA_NOT_FOUND 等）
        return _error_response(exc.status_code, exc.code, exc.message, exc.detail)

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        # 参数校验失败统一为 400 INVALID_ARGUMENT，errors 给出逐字段原因
        return _error_response(
            400,
            "INVALID_ARGUMENT",
            "请求参数校验失败",
            {"errors": _serializable_errors(exc)},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _DEFAULT_CODES.get(exc.status_code, "ERROR")
        message = exc.detail if isinstance(exc.detail, str) else "请求失败"
        return _error_response(exc.status_code, code, message)

    @app.exception_handler(Exception)
    async def _handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
        # 未预期异常记日志（NFR-03：日志脱敏由 utils/text.py 的 mask_secrets 承担）
        logger.exception("未处理异常：%s", exc)
        return _error_response(500, "INTERNAL_ERROR", "服务内部错误")


# --------------------------------------------------------------------------- #
# 依赖：trace_id
# --------------------------------------------------------------------------- #


def get_trace_id() -> str:
    """为每个请求生成 trace_id（NFR-06 可观测）。

    后续若在 main.py 加请求级中间件，可改为从 request.state 读取。
    """

    return new_trace_id()


# --------------------------------------------------------------------------- #
# 依赖：service 层
# --------------------------------------------------------------------------- #
#
# 按需加载，而不是模块顶层 import。原因：路由层不应因为某个 service 尚未实现
# 而无法导入与启动（对应开发文档 R7 的降级原则）。未实现时返回 503，
# 前端与联调方看到的是明确的错误码，而不是启动失败。
#
# 测试与联调可用 FastAPI 的依赖覆盖替换：
#     app.dependency_overrides[get_chat_service] = lambda: FakeChatService()
# --------------------------------------------------------------------------- #


def _load_service(module: str, purpose: str, required: tuple[str, ...] = ()) -> Any:
    """加载 service 模块并校验其实现了约定接口。

    注意：仓库中的 service 文件目前是**空文件**，能 import 成功但没有任何属性。
    因此除了捕获 ImportError，还必须校验 required 里的方法确实存在，
    否则会在调用处爆 AttributeError（500），而不是明确的 503。
    """

    try:
        loaded = importlib.import_module(f"backend.services.{module}")
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.startswith("backend.services"):
            raise ApiError(
                503,
                "SERVICE_NOT_READY",
                f"{purpose}尚未实现（backend/services/{module}.py）",
            ) from exc
        raise

    missing = [name for name in required if not callable(getattr(loaded, name, None))]
    if missing:
        raise ApiError(
            503,
            "SERVICE_NOT_READY",
            f"{purpose}尚未实现（backend/services/{module}.py 缺少 {', '.join(missing)}）",
        )
    return loaded


#: 各 service 必须提供的方法（即本层依赖的契约，见 routers 子模块 docstring）
_CHAT_SERVICE_API = ("stream_answer", "list_sessions", "list_messages", "add_feedback")
_KB_SERVICE_API = (
    "save_image_upload",
    "create_document",
    "rebuild_index",
    "get_job",
    "list_chunks",
)


def get_chat_service() -> Any:
    """问答与会话服务（backend/services/chat_service.py）。"""

    return _load_service("chat_service", "会话与问答服务", _CHAT_SERVICE_API)


def get_kb_service() -> Any:
    """知识库服务（backend/services/kb_service.py）。"""

    return _load_service("kb_service", "知识库服务", _KB_SERVICE_API)


# --------------------------------------------------------------------------- #
# 路由聚合
# --------------------------------------------------------------------------- #

api_router = APIRouter()


def register_routers() -> APIRouter:
    """挂载 chat / admin 两个子路由。"""

    from . import admin, chat

    api_router.include_router(chat.router)
    api_router.include_router(admin.router)
    return api_router


register_routers()

__all__ = [
    "ApiError",
    "api_router",
    "get_chat_service",
    "get_kb_service",
    "get_trace_id",
    "register_error_handlers",
]
