"""考核认证路由（出题 / 判分 / 认证记录 / 到期提醒）。

本模块只做 HTTP 翻译：取参 -> 调 service -> 返回，业务规则全在
`backend/services/training_service.py`（开发文档 6.2 / 6.1 的依赖方向要求）。

service 契约（backend/services/training_service.py 需实现，全部 async）：

    generate_quiz(payload, *, trace_id)                 -> dict（QuizResponse 可校验）

    get_quiz(quiz_id, *, trace_id)                      -> dict | None

    submit_attempt(quiz_id, payload, *, trace_id)        -> dict | None
        # 判分不自动发证：certification_id 恒为 None

    issue_certification(payload, *, trace_id)            -> dict（CertificationResponse）
        # 必须基于一次通过的考核，否则 ServiceError(400, ATTEMPT_NOT_PASSED)

    list_certifications(*, trainee, device_model, status, limit, offset, trace_id) -> dict

    list_expiring(*, days, trace_id)                     -> dict

注意：`/training/certifications/expiring` 是**静态路径**，必须声明在带路径参数的
路由之前（本文件里没有任何 `/{id}` 形态的认证路由，这里仍按该顺序声明，
避免以后新增 `GET /training/certifications/{id}` 时被 `expiring` 抢先匹配）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from ..schemas import (
    CertificationListResponse,
    CertificationRequest,
    CertificationResponse,
    QuizGenerateRequest,
    QuizResponse,
    QuizSubmitRequest,
    QuizSubmitResponse,
)
from . import ApiError, get_trace_id, get_training_service

router = APIRouter(tags=["考核认证"])


@router.post("/training/quizzes", response_model=QuizResponse, summary="生成考核题目")
async def generate_quiz(
    payload: QuizGenerateRequest,
    service: Any = Depends(get_training_service),
    trace_id: str = Depends(get_trace_id),
) -> QuizResponse:
    """按设备型号 / 主题出题（FR 考核认证）。

    题目**只依据知识库检索到的片段**：每道题都带 evidence（文档 / 页码 / 章节），
    检索不到依据或片段里没有可用句子时返回 422 QUIZ_GENERATION_FAILED，
    而不是生成没有依据的题（零幻觉）。
    `generator` 字段标明生成方式：`llm:<model>` 或 `extractive-fallback`（确定性抽句降级）。

    **出题量可能少于 `n_items`**：不合规的题会被剔除且不补题重试，实际题量看 `n_items`，
    缩水情况看 `requested_items` / `dropped_items`（前端应据此提示，不要静默展示）。
    """

    result = await service.generate_quiz(payload, trace_id=trace_id)
    return QuizResponse.model_validate(result)


@router.get("/training/quizzes/{quiz_id}", response_model=QuizResponse, summary="查看试卷")
async def get_quiz(
    quiz_id: int,
    service: Any = Depends(get_training_service),
    trace_id: str = Depends(get_trace_id),
) -> QuizResponse:
    """按 id 取回试卷（含题目、选项、答案与逐题依据）。"""

    result = await service.get_quiz(quiz_id, trace_id=trace_id)
    if result is None:
        raise ApiError(404, "QUIZ_NOT_FOUND", f"试卷不存在：{quiz_id}")
    return QuizResponse.model_validate(result)


@router.post(
    "/training/quizzes/{quiz_id}/submit",
    response_model=QuizSubmitResponse,
    summary="提交作答并判分",
)
async def submit_attempt(
    quiz_id: int,
    payload: QuizSubmitRequest,
    service: Any = Depends(get_training_service),
    trace_id: str = Depends(get_trace_id),
) -> QuizSubmitResponse:
    """提交作答，返回分数、是否通过（及格线 60 分）与逐题批改明细。

    **判分不自动发证**：响应里的 `certification_id` 恒为 None。
    需要发证时再显式调 `POST /training/certifications`（要指定等级与有效期）。
    """

    result = await service.submit_attempt(quiz_id, payload, trace_id=trace_id)
    if result is None:
        raise ApiError(404, "QUIZ_NOT_FOUND", f"试卷不存在：{quiz_id}")
    return QuizSubmitResponse.model_validate(result)


@router.get(
    "/training/certifications/expiring",
    response_model=CertificationListResponse,
    summary="到期 / 即将到期认证提醒",
)
async def list_expiring(
    days: int = Query(default=30, ge=0, le=3650, description="未来多少天内到期"),
    service: Any = Depends(get_training_service),
    trace_id: str = Depends(get_trace_id),
) -> CertificationListResponse:
    """证书到期提醒：未来 `days` 天内到期 + 已过期 30 天内的认证，按到期时间升序。

    `days_to_expiry` 为距到期天数（**负数=已过期**）。
    """

    result = await service.list_expiring(days=days, trace_id=trace_id)
    return CertificationListResponse.model_validate(result)


@router.get(
    "/training/certifications",
    response_model=CertificationListResponse,
    summary="认证记录列表",
)
async def list_certifications(
    trainee: str | None = Query(default=None, description="按被认证人过滤"),
    device_model: str | None = Query(default=None, description="按设备型号过滤"),
    status: str | None = Query(default=None, description="按状态过滤：valid / revoked"),
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    service: Any = Depends(get_training_service),
    trace_id: str = Depends(get_trace_id),
) -> CertificationListResponse:
    """认证记录查询（按签发时间倒序，带总数）。"""

    result = await service.list_certifications(
        trainee=trainee,
        device_model=device_model,
        status=status,
        limit=limit,
        offset=offset,
        trace_id=trace_id,
    )
    return CertificationListResponse.model_validate(result)


@router.post(
    "/training/certifications",
    response_model=CertificationResponse,
    summary="签发认证",
)
async def issue_certification(
    payload: CertificationRequest,
    service: Any = Depends(get_training_service),
    trace_id: str = Depends(get_trace_id),
) -> CertificationResponse:
    """按一次**通过的**考核签发认证，并记录有效期（用于到期提醒）。

    - `attempt_id` 必须是 passed=True 的考核记录，否则 400 ATTEMPT_NOT_PASSED；
    - `level` 为 L1 / L2 / L3（按设备类型分级授权），`valid_days` 为有效期天数；
    - 系统**不代表原厂发证**：`issuer` 默认「内部授权」，note 固定标注该口径。
    """

    result = await service.issue_certification(payload, trace_id=trace_id)
    return CertificationResponse.model_validate(result)
