/**
 * 图片上传接口。
 *
 * 契约依据：接口文档.md §4.2。
 *
 * **两段式**：先上传拿 image_id，再在 /api/chat/stream 的 image_ids 里引用。
 * 本接口只做预处理（压缩 / 格式统一 / 体积校验），**不做识别**——
 * 识别发生在问答链路的 ingest_image 节点，结果经 SSE 的 image 事件返回。
 */

import { ApiError, request } from '@/api/http'

export interface ImageUploadResponse {
  image_id: string
  filename: string
  mime: string
  size_bytes: number
  width: number
  height: number
  /** /static/uploads/img_xxx.png，dev 下由 Vite 代理到 :8000 */
  url: string
  trace_id: string
}

/**
 * 前端预校验阈值。与 backend/setting.py 保持一致：
 *   image_max_mb / image_min_side / image_max_count / image_allowed_mime
 *
 * 预校验只是省一次往返的体验优化，**后端才是权威**：
 * 413 / 415 / 400 错误码仍然要正常映射，不能假设前端拦住了就万事大吉。
 */
export const IMAGE_LIMITS = {
  maxMb: 5,
  maxBytes: 5 * 1024 * 1024,
  minSide: 200,
  maxCount: 3,
  mime: ['image/jpeg', 'image/png', 'image/webp'] as const,
  /** 允许的扩展名，用于 accept 属性与兜底判断（部分浏览器 paste 的 File.type 为空） */
  ext: ['jpg', 'jpeg', 'png', 'webp'] as const,
} as const

/** 读取图片真实像素尺寸。用 createImageBitmap 而非 Image，避免 objectURL 泄漏。 */
async function readImageSize(file: File): Promise<{ width: number; height: number }> {
  if (typeof createImageBitmap === 'function') {
    const bitmap = await createImageBitmap(file)
    try {
      return { width: bitmap.width, height: bitmap.height }
    } finally {
      bitmap.close()
    }
  }
  // 兜底：老浏览器走 Image + objectURL，必须在 finally 里 revoke
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file)
    const img = new Image()
    img.onload = () => {
      URL.revokeObjectURL(url)
      resolve({ width: img.naturalWidth, height: img.naturalHeight })
    }
    img.onerror = () => {
      URL.revokeObjectURL(url)
      reject(new ApiError({ code: 'UNSUPPORTED_IMAGE_TYPE', message: '图片无法解码，请换一张。' }))
    }
    img.src = url
  })
}

/**
 * 客户端预校验，失败时抛与后端同码的 ApiError，便于统一提示。
 *
 * 顺序刻意与后端一致：空文件 → 体积 → 格式 → 最小边。
 * 先判体积再解码尺寸，避免对超大文件做无谓的解码。
 */
export async function validateImage(file: File): Promise<void> {
  if (!file || file.size === 0) {
    throw new ApiError({ code: 'EMPTY_FILE', message: '文件为空，请重新选择。' })
  }

  if (file.size > IMAGE_LIMITS.maxBytes) {
    throw new ApiError({
      code: 'IMAGE_TOO_LARGE',
      status: 413,
      message: `图片超过 ${IMAGE_LIMITS.maxMb}MB 限制（当前 ${(file.size / 1024 / 1024).toFixed(1)}MB）。`,
      detail: { max_mb: IMAGE_LIMITS.maxMb, actual_mb: Number((file.size / 1024 / 1024).toFixed(2)) },
    })
  }

  // 粘贴的截图可能没有 type，此时靠扩展名兜底；两者都没有则直接拒绝
  const ext = file.name.split('.').pop()?.toLowerCase() ?? ''
  const mimeOk = (IMAGE_LIMITS.mime as readonly string[]).includes(file.type)
  const extOk = (IMAGE_LIMITS.ext as readonly string[]).includes(ext)
  if (!mimeOk && !extOk) {
    throw new ApiError({
      status: 415,
      code: 'UNSUPPORTED_IMAGE_TYPE',
      message: '图片格式不支持，仅允许 jpg / jpeg / png / webp。',
    })
  }

  let size: { width: number; height: number }
  try {
    size = await readImageSize(file)
  } catch (error) {
    if (error instanceof ApiError) throw error
    throw new ApiError({ code: 'UNSUPPORTED_IMAGE_TYPE', message: '图片无法解码，请换一张。' })
  }

  if (Math.min(size.width, size.height) < IMAGE_LIMITS.minSide) {
    throw new ApiError({
      status: 400,
      code: 'IMAGE_TOO_SMALL',
      message: `图片过小（${size.width}×${size.height}），最小边需 ${IMAGE_LIMITS.minSide}px，识别质量不可用。`,
      detail: { min_side: IMAGE_LIMITS.minSide, width: size.width, height: size.height },
    })
  }
}

/** 上传图片（接口文档 §4.2），返回 image_id 供提问时引用。 */
export function uploadImage(file: File, sessionId?: string | null): Promise<ImageUploadResponse> {
  const formData = new FormData()
  formData.append('file', file, file.name)
  if (sessionId) formData.append('session_id', sessionId)

  return request<ImageUploadResponse>('/upload/image', {
    method: 'POST',
    formData,
    // 上传与图片压缩耗时更长，给到 60s
    timeoutMs: 60_000,
  })
}
