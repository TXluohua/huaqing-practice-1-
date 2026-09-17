"""服务层：会话/问答（chat_service）与知识库（kb_service）。

分层约定（开发文档 6.1）：routers -> services -> agents/tools/rag -> config。
本层不碰 HTTP 细节：SSE 文本块的拼装是唯一例外（开发文档 6.2 明确
chat_service 负责「包 SSE 响应」），错误统一用 schemas.ServiceError 表达。
"""

__all__: list[str] = []
