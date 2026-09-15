#!/usr/bin/env python
"""本地向量模型准备与自检（向量化全部在本地跑，不调用任何外部 API）。

用法
    .venv/bin/python scripts/prepare_model.py                     # 检查本地模型是否就绪（默认）
    .venv/bin/python scripts/prepare_model.py --check             # 同上，并做语义/判别力自检
    .venv/bin/python scripts/prepare_model.py --copy-from-cache   # 从本机 HF 缓存复制到项目模型目录
    .venv/bin/python scripts/prepare_model.py --download          # 联网下载（走 HF_ENDPOINT，默认镜像）
    .venv/bin/python scripts/prepare_model.py --download --source modelscope   # 换 ModelScope 源
    .venv/bin/python scripts/prepare_model.py --download --with-reranker
    .venv/bin/python scripts/prepare_model.py --model BAAI/bge-m3 --download
    .venv/bin/python scripts/prepare_model.py --list              # 列出项目模型目录里已有什么

设计要点
--------
1. **模型落在项目内**：`HF_HOME` 指向 `setting.model_dir`（默认 `backend/data/models`），
   不写用户家目录，因此 Docker 构建 / 换机只需同步这一个目录；
2. **默认离线**：加载模型时 `HF_HUB_OFFLINE=1`，不会偷偷联网；只有本脚本 `--download`
   才临时打开下载；
3. **自检不是形式**：embedding 用「相关句对相似度 > 无关句对」验证语义是否真的生效；
   精排用探针排序验证判别力 —— 因为 sentence-transformers 在拿不到权重时会
   静默造一个未训练模型，打分毫无区分度却看着能用（本项目实测踩到过）。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.rag.store import (  # noqa: E402
    HashingEmbedder,
    SentenceTransformerEmbedder,
    configure_hf_env,
    resolve_model_ref,
)
from backend.setting import get_settings  # noqa: E402

#: 语义自检探针：(查询, 相关文本, 无关文本)
EMBED_PROBES: tuple[tuple[str, str, str], ...] = (
    (
        "刻蚀机腔体真空度异常怎么排查",
        "腔体真空度出现异常波动时，检查腔体门 O 型密封圈有无压痕与开裂。",
        "本章介绍公司员工的考勤制度、报销流程与年假申请方式。",
    ),
    (
        "真空泵抽速标准",
        "干泵 RP-300 额定抽速 300 立方米每小时，分子泵 TMP-1600 转速 42000 转每分钟。",
        "今日天气晴朗，气温适宜，适合户外郊游与野餐活动。",
    ),
)

RERANK_PROBES: tuple[tuple[str, str, str], ...] = (
    (
        "刻蚀腔体清洗周期是多少",
        "抹布清洗周期为每 25 片晶圆执行一次原位等离子清洗。",
        "本章介绍设备采购合同的付款条款与验收流程。",
    ),
)


def _cache_repo_dir(repo: str) -> Path:
    """本机 HF 缓存里某仓库的目录（models--<org>--<name>）。"""

    cache_root = Path(os.environ.get("HF_HUB_CACHE") or (Path.home() / ".cache/huggingface/hub"))
    return cache_root / ("models--" + repo.replace("/", "--"))


def _user_cache_repo_dir(repo: str) -> Path:
    """用户家目录 HF 缓存（--copy-from-cache 的来源）。"""

    return Path.home() / ".cache" / "huggingface" / "hub" / ("models--" + repo.replace("/", "--"))


def _latest_snapshot(repo_dir: Path) -> Path | None:
    snapshots = repo_dir / "snapshots"
    if not snapshots.exists():
        return None
    candidates = [p for p in snapshots.iterdir() if p.is_dir()]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _local_model_ready(repo: str, model_dir: Path) -> bool:
    """项目模型目录里是否已有该仓库（按 HF 缓存布局判断）。"""

    repo_dir = model_dir / "hub" / ("models--" + repo.replace("/", "--"))
    snapshot = _latest_snapshot(repo_dir)
    if snapshot is None:
        return False
    names = {p.name for p in snapshot.iterdir()}
    return bool(names & {"model.safetensors", "pytorch_model.bin", "model.onnx", "openvino_model.bin"})


def cmd_list(args: argparse.Namespace) -> int:
    settings = get_settings()
    configure_hf_env(settings)
    model_dir = Path(settings.model_dir)
    print("=" * 78)
    print("项目模型目录")
    print("=" * 78)
    print(f"  路径          : {model_dir}")
    print(f"  HF_HUB_CACHE  : {os.environ.get('HF_HUB_CACHE')}")
    print(f"  离线加载      : {os.environ.get('HF_HUB_OFFLINE') == '1'}")
    print(f"  HF_ENDPOINT   : {os.environ.get('HF_ENDPOINT') or '（未设置，用官方地址）'}")
    def _dir_size(path: Path) -> float:
        return sum(
            f.stat().st_size for f in path.rglob("*") if f.is_file() and not f.is_symlink()
        ) / 1e9

    entries: list[tuple[str, str, float]] = []
    hub = model_dir / "hub"
    if hub.exists():
        for repo_dir in sorted(hub.glob("models--*")):
            entries.append((repo_dir.name.replace("models--", "").replace("--", "/"), "HF 缓存布局", _dir_size(repo_dir)))
    # 普通目录形式（ModelScope / 手工拷贝）：只要有权重文件就算已部署
    for local_dir in sorted(model_dir.iterdir()) if model_dir.exists() else []:
        if not local_dir.is_dir() or local_dir.name in {"hub"} or local_dir.name.startswith("ms-"):
            continue
        has_weight = any(local_dir.glob("*.safetensors")) or (local_dir / "pytorch_model.bin").exists()
        if has_weight:
            entries.append((local_dir.name, "目录布局", _dir_size(local_dir)))

    if not entries:
        print("  已部署模型    : （空 —— 请先运行 --copy-from-cache 或 --download）")
    else:
        print("  已部署模型    :")
        for name, layout, size in entries:
            print(f"    - {name}（{size:.2f} GB，{layout}）")
    print("=" * 78)
    return 0


def cmd_copy_from_cache(args: argparse.Namespace) -> int:
    settings = get_settings()
    configure_hf_env(settings)
    target_root = Path(settings.model_dir) / "hub"
    target_root.mkdir(parents=True, exist_ok=True)

    repos = list(dict.fromkeys([args.model, *(args.extra_models or [])]))
    if args.with_reranker:
        repos.append(settings.rerank_model)

    copied = 0
    for repo in repos:
        source = _user_cache_repo_dir(repo)
        if not source.exists():
            print(f"  ✗ {repo}：本机 HF 缓存里没有（{source}）")
            continue
        target = target_root / source.name
        # symlinks=True：缓存里 snapshots 指向 blobs 的相对软链会被原样复制，
        # 因此不会把权重文件重复复制一遍（省空间且保持布局）
        shutil.copytree(source, target, symlinks=True, dirs_exist_ok=True)
        size = sum(
            f.stat().st_size for f in target.rglob("*") if f.is_file() and not f.is_symlink()
        )
        print(f"  ✓ {repo} → {target}（{size / 1e9:.2f} GB，已解引用软链可离线加载）")
        copied += 1
    print(f"\n复制完成：{copied}/{len(repos)} 个模型已落到项目目录 {target_root}")
    return 0 if copied else 1


def _download_from_modelscope(repo: str, model_dir: Path) -> str:
    """从 ModelScope 下载到项目模型目录（HF 镜像不可用时的退路）。

    ModelScope 的 SDK 会把配置写进 `~/.modelscope`，在受限环境（或容器只读家目录）会失败，
    因此这里把 SDK 目录与缓存目录一并重定向到项目内。
    """

    os.environ.setdefault("MODELSCOPE_HOME", str(model_dir / "ms-home"))
    os.environ.setdefault("MODELSCOPE_CACHE", str(model_dir / "ms-cache"))
    try:
        from modelscope import snapshot_download as ms_snapshot_download
    except ImportError as exc:  # pragma: no cover - 依赖缺失时的指引
        raise RuntimeError(
            "需要 modelscope：.venv/bin/python -m pip install modelscope"
        ) from exc
    target = model_dir / Path(repo).name
    ms_snapshot_download(repo, local_dir=str(target))
    return str(target)


def cmd_download(args: argparse.Namespace) -> int:
    settings = get_settings()
    model_dir = Path(settings.model_dir)
    # 下载必须联网：显式关掉离线标志
    configure_hf_env(settings, offline=False)
    try:
        from huggingface_hub import snapshot_download
    except ImportError:  # pragma: no cover - 依赖缺失时的指引
        print("需要 huggingface_hub：.venv/bin/python -m pip install huggingface_hub")
        return 2

    repos = list(dict.fromkeys([args.model, *(args.extra_models or [])]))
    if args.with_reranker:
        repos.append(settings.rerank_model)

    print(f"来源：{args.source}")
    if args.source == "hf":
        print(f"端点：{os.environ.get('HF_ENDPOINT') or 'https://huggingface.co'}")
        print(f"目标：{os.environ.get('HF_HUB_CACHE')}")
    else:
        print(f"目标：{model_dir}（ModelScope 下载为普通目录，按路径直接加载）")
    print()

    failed = 0
    for repo in repos:
        try:
            if args.source == "modelscope":
                target = _download_from_modelscope(repo, model_dir)
                print(f"  ✓ {repo} 下载完成 → {target}")
            else:
                snapshot_download(repo_id=repo, cache_dir=os.environ.get("HF_HUB_CACHE"))
                print(f"  ✓ {repo} 下载完成")
        except Exception as exc:  # noqa: BLE001 - 单个模型失败不阻断其余
            failed += 1
            message = str(exc)[:160]
            print(f"  ✗ {repo} 下载失败：{type(exc).__name__}: {message}")
            if "401" in message or "CAS" in message.upper():
                print("    提示：镜像的大文件跳转被拒（401 CAS），可改用 `--source modelscope`")
    return 1 if failed else 0


def cmd_check(args: argparse.Namespace) -> int:
    settings = get_settings()
    configure_hf_env(settings)
    print("=" * 78)
    print("本地模型自检（向量化不调用任何外部 API）")
    print("=" * 78)
    print(f"  provider      : {settings.embedding_provider}")
    print(f"  embedding 模型: {settings.embedding_model}")
    print(f"  模型目录      : {settings.model_dir}")
    print(f"  离线          : {settings.embedding_offline}")
    print("-" * 78)

    status = 0

    # ---- 1) embedding ----
    embedder = SentenceTransformerEmbedder(
        settings.embedding_model, offline=settings.embedding_offline
    )
    try:
        # 按固定顺序编码并记录每组探针的起始下标，避免下标错位把「相关/无关」比反
        texts: list[str] = []
        starts: list[int] = []
        for query, relevant, irrelevant in EMBED_PROBES:
            starts.append(len(texts))
            texts.extend([query, relevant, irrelevant])
        vectors = asyncio.run(embedder.embed(texts))
        dim = int(vectors.shape[1])
        print(f"  ✓ embedding 加载成功：{settings.embedding_model}（{dim} 维，签名 {embedder.signature}）")
        for (query, _relevant, _irrelevant), start in zip(EMBED_PROBES, starts):
            sim_rel = float(vectors[start] @ vectors[start + 1])
            sim_irr = float(vectors[start] @ vectors[start + 2])
            ok = sim_rel > sim_irr
            status |= 0 if ok else 1
            print(
                f"    {'✓' if ok else '✗'} 语义探针：「{query[:16]}…」"
                f" 相关 {sim_rel:.3f} vs 无关 {sim_irr:.3f}"
                + ("" if ok else " ← 语义未生效，模型可能未训练或不匹配")
            )
    except Exception as exc:  # noqa: BLE001 - 自检失败要给出可执行指引
        status |= 1
        print(f"  ✗ embedding 加载失败：{type(exc).__name__}: {str(exc)[:200]}")
        print("    修复：--copy-from-cache（用本机缓存）或 --download（联网下载到项目目录）")

    # ---- 2) 精排（可选）----
    print("-" * 78)
    try:
        from sentence_transformers import CrossEncoder

        rerank_ref = resolve_model_ref(settings.rerank_model, settings.model_dir)
        model = CrossEncoder(rerank_ref)
        pairs: list[tuple[str, str]] = []
        for query, relevant, irrelevant in RERANK_PROBES:
            pairs.extend([(query, relevant), (query, irrelevant)])
        scores = [float(x) for x in model.predict(pairs)]
        ok = all(scores[i] > scores[i + 1] for i in range(0, len(scores) - 1, 2))
        status |= 0 if ok else 1
        print(
            f"  {'✓' if ok else '✗'} 精排模型 {settings.rerank_model}（{rerank_ref}）："
            f"相关 {scores[0]:.3f} vs 无关 {scores[1]:.3f}"
            + ("" if ok else " ← 判别力不足，运行时会自动降级为词法精排（lexical）")
        )
    except Exception as exc:  # noqa: BLE001 - 精排缺失不阻塞向量化
        print(
            f"  ⚠ 精排模型不可用（{type(exc).__name__}）：将降级为词法精排（lexical）。"
            f"需要真 Cross-Encoder 可运行 --download --with-reranker"
        )

    # ---- 3) 兜底可用性（仅提示）----
    print("-" * 78)
    hashing_dim = HashingEmbedder().dim
    print(f"  兜底：hashing（{hashing_dim} 维）始终可用；仅在 EMBEDDING_PROVIDER=auto/hashing 时启用")
    print("=" * 78)
    if status == 0:
        print("结论：本地向量模型就绪，可执行 `scripts/build_index.py` 重建索引。")
    else:
        print("结论：有检查未通过，请按上面的修复指引处理（不要直接入库，否则质量不可信）。")
    return status


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="本地向量模型准备与自检（不调用外部 API）")
    parser.add_argument("--check", action="store_true", help="检查并自检本地模型（默认动作）")
    parser.add_argument("--list", action="store_true", help="列出项目模型目录里已部署的模型")
    parser.add_argument(
        "--copy-from-cache",
        dest="copy_from_cache",
        action="store_true",
        help="从本机 HF 缓存复制到项目模型目录（离线可用）",
    )
    parser.add_argument("--download", action="store_true", help="联网下载到项目模型目录")
    parser.add_argument(
        "--source",
        choices=["hf", "modelscope"],
        default="hf",
        help="下载源：hf（走 HF_ENDPOINT 镜像，默认）/ modelscope",
    )
    parser.add_argument("--with-reranker", action="store_true", help="同时处理精排模型")
    parser.add_argument("--model", default=None, help="指定模型仓库名（缺省用 EMBEDDING_MODEL）")
    parser.add_argument("--extra-models", nargs="*", default=None, help="额外模型列表")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    settings.ensure_dirs()
    if not args.model:
        args.model = settings.embedding_model

    if args.list:
        return cmd_list(args)
    if args.copy_from_cache:
        return cmd_copy_from_cache(args)
    if args.download:
        return cmd_download(args)
    return cmd_check(args)


if __name__ == "__main__":
    raise SystemExit(main())
