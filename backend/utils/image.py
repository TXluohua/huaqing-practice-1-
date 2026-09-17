"""图片预处理（开发文档 6.2：压缩 / 格式统一 / 体积校验）。

对应开发文档 5.2 FR-02 的上传侧：**本模块只做预处理，不做识别**。
识别在问答链路的 ingest_image 节点（tools.kb_tools.vision_extract）里执行。

服务层约定：图片落盘为 {upload_dir}/{image_id}.{ext}，
vision_extract 就是按这个约定回查文件的。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from starlette.concurrency import run_in_threadpool

from ..schemas import ServiceError
from ..setting import get_settings

logger = logging.getLogger(__name__)

#: 允许的 MIME 与其落盘扩展名
_MIME_EXT: dict[str, str] = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}

#: 一次最多读入的分片大小
_CHUNK = 64 * 1024


@dataclass(slots=True)
class StoredImage:
    """落盘后的图片信息（字段与 ImageUploadResponse 一一对应）。"""

    image_id: str
    filename: str
    mime: str
    size_bytes: int
    width: int
    height: int
    url: str


async def _read_limited(upload: Any, max_bytes: int) -> bytes:
    """分片读取上传内容，超过上限立即中止（不把超大文件读进内存）。"""

    buffered: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise ServiceError(
                413,
                "IMAGE_TOO_LARGE",
                f"图片超过 {max_bytes / 1024 / 1024:.1f}MB 限制",
                {"max_mb": round(max_bytes / 1024 / 1024, 1), "actual_mb": round(total / 1024 / 1024, 2)},
            )
        buffered.append(chunk)
    if total == 0:
        raise ServiceError(400, "EMPTY_FILE", "上传内容为空")
    return b"".join(buffered)


def _process_image(data: bytes, *, mime: str, min_side: int, max_side: int, quality: int) -> tuple[bytes, int, int]:
    """校验、按需等比压缩并重新编码（CPU 密集，放到线程池里跑）。"""

    from io import BytesIO

    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(BytesIO(data)) as probe:
            probe.verify()  # 只验完整性，verify 之后对象不可再用
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ServiceError(415, "UNSUPPORTED_IMAGE_TYPE", "无法解析该图片文件") from exc

    with Image.open(BytesIO(data)) as img:
        img.load()
        width, height = img.size
        if min(width, height) < min_side:
            raise ServiceError(
                400,
                "IMAGE_TOO_SMALL",
                f"图片尺寸过小（最短边需 >= {min_side}px），识别质量不可用",
                {"width": width, "height": height, "min_side": min_side},
            )
        # 超过上限则等比缩小，避免下游 VLM 与前端预览负担
        if max(width, height) > max_side:
            img.thumbnail((max_side, max_side), Image.LANCZOS)
            width, height = img.size

        buffer = BytesIO()
        if mime == "image/png":
            img.save(buffer, format="PNG", optimize=True)
        elif mime == "image/webp":
            img.save(buffer, format="WEBP", quality=quality)
        else:
            img.convert("RGB").save(buffer, format="JPEG", quality=quality, optimize=True)
        return buffer.getvalue(), width, height


async def save_image_upload(upload: Any, *, session_id: str | None = None, image_id: str | None = None) -> StoredImage:
    """校验并落盘一张用户上传的图片，返回 image_id 与访问 URL。

    参数
    ----
    upload:
        FastAPI 的 UploadFile（本模块只用其 read / filename / content_type）。
    session_id:
        关联会话，仅用于日志排查（历史回看靠 qa_record.image_ids）。
    image_id:
        指定 ID（测试用）；缺省自动生成 img_xxxxxxxx。
    """

    settings = get_settings()
    settings.ensure_dirs()

    resolved_mime = (getattr(upload, "content_type", "") or "").lower()
    if resolved_mime not in _MIME_EXT:
        # 兼容部分客户端把 content-type 传成 octet-stream 的情况：退回按扩展名判断
        suffix = Path(str(getattr(upload, "filename", "") or "")).suffix.lower().lstrip(".")
        guess = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}.get(suffix)
        if guess is None:
            raise ServiceError(
                415,
                "UNSUPPORTED_IMAGE_TYPE",
                "仅支持 jpg / png / webp 格式的图片",
                {"content_type": resolved_mime or None, "allowed": sorted(set(_MIME_EXT))},
            )
        resolved_mime = guess

    if resolved_mime not in settings.image_allowed_mime:
        raise ServiceError(
            415,
            "UNSUPPORTED_IMAGE_TYPE",
            "仅支持 jpg / png / webp 格式的图片",
            {"content_type": resolved_mime, "allowed": list(settings.image_allowed_mime)},
        )

    max_bytes = int(settings.image_max_mb * 1024 * 1024)
    raw = await _read_limited(upload, max_bytes)

    processed, width, height = await run_in_threadpool(
        _process_image,
        raw,
        mime=resolved_mime,
        min_side=settings.image_min_side,
        max_side=settings.image_max_side,
        quality=settings.image_jpeg_quality,
    )

    resolved_id = image_id or f"img_{uuid.uuid4().hex[:8]}"
    ext = _MIME_EXT[resolved_mime]
    filename = f"{resolved_id}.{ext}"
    path = Path(settings.upload_dir) / filename
    await run_in_threadpool(path.write_bytes, processed)

    logger.info(
        "图片已保存 image_id=%s session=%s %dx%d %.1fKB->%.1fKB",
        resolved_id,
        session_id or "-",
        width,
        height,
        len(raw) / 1024,
        len(processed) / 1024,
    )
    return StoredImage(
        image_id=resolved_id,
        filename=str(getattr(upload, "filename", "") or filename),
        mime=resolved_mime,
        size_bytes=len(processed),
        width=width,
        height=height,
        url=f"{settings.static_url_prefix}/uploads/{filename}",
    )
