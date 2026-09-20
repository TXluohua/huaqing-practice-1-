#!/usr/bin/env python
"""生成 `data/eval/golden_image.jsonl` 需要的 3 张图片 fixture（收尾任务 T3）。

用法
    .venv/bin/python scripts/make_image_fixtures.py          # 生成到 data/eval/fixtures/
    .venv/bin/python scripts/make_image_fixtures.py --check  # 只检查是否齐备

为什么用脚本而不是直接放 3 个 png 进仓库
    图片内容必须与 `golden_image.jsonl` 的 `expect_extract` 逐字对齐（报警码 E-2041、
    780 ℃ / 300 Pa / 350 sccm），手画容易漂。脚本化之后，改期望值只需改常量再重跑，
    评审也能看出图里到底写了什么，而不是面对一个不透明的二进制文件。

关于中文字体
    镜像里通常没有 CJK 字体，脚本按候选列表探测（含 WSL 下的 /mnt/c/Windows/Fonts）。
    一个都找不到时**直接报错退出**，不退回默认位图字体 —— 那样渲出来的中文是方块，
    VLM 识别不出，问题会被误判成「模型不行」而不是「环境缺字体」。

img003 的特别之处
    它的期望是「**识别不出来**」：用于验证「不允许猜测」。所以故意渲染成低对比、
    倾斜、带噪点的模糊纸条，让 VLM 只能回「无法识别」。**不要**为了好看把这张图修清晰，
    那样会把一条负样本变成正样本，评测结论直接反过来。
"""

from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "data" / "eval" / "fixtures"

#: 中文字体候选。按「越靠前越优先」探测，覆盖 Linux / WSL / macOS。
FONT_CANDIDATES: tuple[tuple[str, int], ...] = (
    ("/mnt/c/Windows/Fonts/simhei.ttf", 0),
    ("/mnt/c/Windows/Fonts/msyh.ttc", 0),
    ("/mnt/c/Windows/Fonts/simsun.ttc", 0),
    ("/mnt/c/Windows/Fonts/STKAITI.TTF", 0),
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 0),
    ("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", 0),
    ("/usr/share/fonts/truetype/arphic/uming.ttc", 0),
    ("/System/Library/Fonts/PingFang.ttc", 0),
    ("/Library/Fonts/Arial Unicode.ttf", 0),
)

#: 手写体候选（img003 用），找不到就退回上面第一个可用字体
HANDWRITING_CANDIDATES: tuple[tuple[str, int], ...] = (
    ("/mnt/c/Windows/Fonts/STKAITI.TTF", 0),
    ("/mnt/c/Windows/Fonts/simkai.ttf", 0),
    ("/System/Library/Fonts/Supplemental/Kaiti.ttc", 0),
)


def find_font(candidates: tuple[tuple[str, int], ...]) -> tuple[str, int] | None:
    for path, index in candidates:
        if Path(path).is_file():
            return path, index
    return None


def load(path: str, index: int, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size=size, index=index)


# --------------------------------------------------------------------------- #
# img001 报警弹窗：要有报警码 E-2041 + 「腔体压力异常」字样
# --------------------------------------------------------------------------- #
def render_alarm(path: Path, font_path: str, font_index: int) -> None:
    W, H = 760, 420
    img = Image.new("RGB", (W, H), (18, 22, 28))
    d = ImageDraw.Draw(img)

    # 顶部标题栏，模拟 HMI 弹窗
    d.rectangle([0, 0, W, 52], fill=(148, 32, 32))
    d.text((20, 14), "设备报警 · Etcher-A", font=load(font_path, font_index, 22), fill=(255, 255, 255))

    # 报警码用超大字号，保证 VLM 一定能读到 E-2041
    d.text((36, 84), "E-2041", font=load(font_path, font_index, 72), fill=(255, 82, 82))

    d.text(
        (40, 182),
        "腔体压力异常",
        font=load(font_path, font_index, 34),
        fill=(255, 214, 102),
    )
    d.text(
        (40, 232),
        "腔体压力连续 3 s 超过工艺设定值的 150%",
        font=load(font_path, font_index, 19),
        fill=(214, 222, 233),
    )

    d.line([36, 272, W - 36, 272], fill=(58, 66, 78), width=1)
    d.text((40, 288), "发生时间   2026-09-18 14:26:31", font=load(font_path, font_index, 18), fill=(150, 160, 175))
    d.text((40, 318), "设备状态   已停机  腔体压力 68.4 Pa", font=load(font_path, font_index, 18), fill=(150, 160, 175))

    # 两个按钮：纯装饰，让截图更像真实 HMI
    d.rectangle([W - 250, 350, W - 140, 392], fill=(38, 46, 58), outline=(70, 80, 96))
    d.text((W - 232, 361), "查看详情", font=load(font_path, font_index, 18), fill=(200, 210, 225))
    d.rectangle([W - 128, 350, W - 36, 392], fill=(0, 122, 204))
    d.text((W - 110, 361), "确认", font=load(font_path, font_index, 18), fill=(255, 255, 255))

    img.save(path)


# --------------------------------------------------------------------------- #
# img002 参数表：沉积温度 780 ℃ / 腔体压力 300 Pa / SiH4 流量 350 sccm
# --------------------------------------------------------------------------- #
PARAM_ROWS: tuple[tuple[str, str], ...] = (
    ("沉积温度", "780 ℃"),
    ("腔体压力", "300 Pa"),
    ("SiH4 流量", "350 sccm"),
    ("NH3 流量", "120 sccm"),
    ("射频功率", "450 W"),
    ("沉积时间", "180 s"),
)


def render_param_table(path: Path, font_path: str, font_index: int) -> None:
    W, H = 700, 480
    img = Image.new("RGB", (W, H), (255, 255, 255))
    d = ImageDraw.Draw(img)

    d.text((28, 22), "CVD-200 薄膜沉积工艺参数表", font=load(font_path, font_index, 24), fill=(20, 24, 30))
    d.line([28, 62, W - 28, 62], fill=(180, 190, 200), width=2)

    top, row_h = 84, 52
    header_font = load(font_path, font_index, 20)
    cell_font = load(font_path, font_index, 20)

    # 表头
    d.rectangle([28, top, W - 28, top + row_h], fill=(238, 242, 247), outline=(150, 160, 172))
    d.text((48, top + 15), "参数名称", font=header_font, fill=(30, 36, 44))
    d.text((360, top + 15), "工艺设定值", font=header_font, fill=(30, 36, 44))

    for i, (name, value) in enumerate(PARAM_ROWS):
        y = top + row_h * (i + 1)
        # 隔行浅灰底，避免长表串行
        if i % 2 == 0:
            d.rectangle([28, y, W - 28, y + row_h], fill=(250, 251, 253))
        d.rectangle([28, y, W - 28, y + row_h], outline=(196, 204, 214))
        d.line([340, y, 340, y + row_h], fill=(196, 204, 214))
        d.text((48, y + 15), name, font=cell_font, fill=(34, 40, 48))
        d.text((360, y + 15), value, font=cell_font, fill=(20, 60, 130))

    d.text(
        (28, top + row_h * (len(PARAM_ROWS) + 1) + 18),
        "注：以上为 LPCVD 氮化硅标准工艺窗口，超窗口需工艺工程师审批。",
        font=load(font_path, font_index, 15),
        fill=(120, 130, 145),
    )

    img.save(path)


# --------------------------------------------------------------------------- #
# img003 模糊手写纸条：**故意识别不出**，用于验证「不允许猜测」
# --------------------------------------------------------------------------- #
#: 纸条上的内容。**不要求被识别**，这里写什么都行 —— 渲染后看不清才是重点。
NOTE_TEXT = ("腔体压力偏高\n先查 O-ring\n再看干泵\n14:30 交班")


def render_handwritten_note(path: Path, font_path: str, font_index: int, seed: int = 20260918) -> None:
    """模糊手写纸条：目标是让 VLM **读不出具体内容**，人眼仍看出「有字的纸条」。

    退化链（按顺序叠加，缺一不可）：
        超采样渲染 → 大幅降采样 → 旋转 → 高斯模糊 → 降对比 → 过曝 → 噪点

    为什么要「超采样 + 降采样」这一步：单纯调大模糊半径，笔画会糊成一团均匀的灰，
    反而容易被当成「无文字」；而先在 3 倍尺寸渲染再缩回去，笔画会断成不连续的墨点，
    正是真实失焦照片的样子 —— 也是最难被 VLM 硬凑出字的形态。
    `SS` 与 `BLUR` 这两个常量是调出来的：早先一版（BLUR=3.4、无降采样）被 qwen-vl-max
    读出了「腔体压力偏高 / 先查O-ring / 1430」并给出 0.85 置信度，负样本失效。
    改动后**必须**重跑 `.venv/bin/python scripts/eval_image.py --ids img003` 复核。
    """

    rnd = random.Random(seed)
    W, H = 720, 420
    SS = 3  # 超采样倍数
    BLUR = 2.6  # 缩回目标尺寸后的高斯半径

    # 1) 在 3 倍画布上写字：细节留在这里，稍后整体丢弃
    big = Image.new("RGBA", (W * SS, H * SS), (0, 0, 0, 0))
    ld = ImageDraw.Draw(big)
    # 刻意用偏小的字号：真实纸条在失焦照片里笔画本就只有几个像素宽
    note_font = load(font_path, font_index, 26 * SS)
    y = 62 * SS
    for line in NOTE_TEXT.split("\n"):
        # 每行起始 x 抖动、字号轻微随机，模拟手写的不整齐
        jitter = rnd.uniform(-0.06, 0.06)
        ld.text(
            (rnd.randint(58, 96) * SS, y),
            line,
            font=load(font_path, font_index, int(26 * SS * (1 + jitter))),
            fill=(66, 62, 56, 255),
        )
        y += 76 * SS

    # 2) 旋转 + 缩回目标尺寸 —— 这一步会不可逆地丢掉笔画细节
    big = big.rotate(rnd.uniform(-5.5, -3.5), resample=Image.BICUBIC, expand=False)
    layer = big.resize((W, H), Image.LANCZOS)

    # 3) 纸张底色（灰黄，带噪点）与文字层合成
    paper = Image.new("RGB", (W, H), (208, 202, 188))
    px = paper.load()
    for yy in range(H):
        for xx in range(W):
            n = rnd.randint(-20, 20)
            r, g, b = px[xx, yy]
            px[xx, yy] = (r + n, g + n, b + n)

    # 文字层整体压低不透明度：纸条是铅笔写的，本身就淡
    alpha = layer.getchannel("A").point(lambda v: int(v * 0.62))
    layer.putalpha(alpha)
    img = Image.alpha_composite(paper.convert("RGBA"), layer).convert("RGB")

    # 4) 模糊 + 降对比：抹掉剩余可辨认的轮廓
    img = img.filter(ImageFilter.GaussianBlur(radius=BLUR))
    base = Image.new("RGB", (W, H), (202, 197, 186))
    # alpha 越大越接近纯底色。0.46 是「字还看得出痕迹、但认不出内容」的位置
    img = Image.blend(img, base, alpha=0.46)

    # 5) 过曝 + 噪点：模拟暗光拍摄的白平衡漂移，进一步压掉残存对比
    img = Image.blend(img, Image.new("RGB", (W, H), (255, 255, 255)), alpha=0.12)
    px = img.load()
    for yy in range(H):
        for xx in range(W):
            n = rnd.randint(-16, 16)
            r, g, b = px[xx, yy]
            px[xx, yy] = (
                max(0, min(255, r + n)),
                max(0, min(255, g + n)),
                max(0, min(255, b + n)),
            )

    img.save(path)


# --------------------------------------------------------------------------- #
# img004 CVD 报警（E-3061 温度超限）：对应《CVD 薄膜沉积工艺规范》#3.1
# --------------------------------------------------------------------------- #
def render_cvd_alarm(path: Path, font_path: str, font_index: int) -> None:
    W, H = 760, 400
    img = Image.new("RGB", (W, H), (16, 20, 26))
    d = ImageDraw.Draw(img)

    d.rectangle([0, 0, W, 50], fill=(150, 96, 20))
    d.text((20, 13), "工艺报警 · CVD-200", font=load(font_path, font_index, 22), fill=(255, 255, 255))

    d.text((36, 80), "E-3061", font=load(font_path, font_index, 68), fill=(255, 168, 60))
    d.text((40, 172), "温度超限", font=load(font_path, font_index, 32), fill=(255, 214, 102))
    d.text(
        (40, 220),
        "实测温度偏离设定值 ±8 ℃ 时触发",
        font=load(font_path, font_index, 19),
        fill=(214, 222, 233),
    )

    d.line([36, 258, W - 36, 258], fill=(58, 66, 78), width=1)
    d.text((40, 274), "设定温度   780 ℃", font=load(font_path, font_index, 18), fill=(150, 160, 175))
    d.text((40, 304), "实测温度   800.6 ℃        已暂停批次", font=load(font_path, font_index, 18), fill=(150, 160, 175))
    d.text((40, 334), "发生时间   2026-09-18 09:41:07", font=load(font_path, font_index, 18), fill=(150, 160, 175))

    img.save(path)


# --------------------------------------------------------------------------- #
# img005 备件标签（腔体门 O-ring）：对应《备件目录与替代件说明》#1.2
# --------------------------------------------------------------------------- #
LABEL_ROWS: tuple[tuple[str, str], ...] = (
    ("品名", "腔体门 O-ring"),
    ("编码", "SP-ETA-0101"),
    ("材质", "FKM"),
    ("硬度", "70 Shore A"),
    ("内径 × 线径", "320 mm × 5.33 mm"),
    ("耐温", "-20~200 ℃"),
    ("更换周期", "3 个月或每次开腔维护后"),
)


def render_parts_label(path: Path, font_path: str, font_index: int) -> None:
    W, H = 720, 500
    img = Image.new("RGB", (W, H), (250, 250, 248))
    d = ImageDraw.Draw(img)

    # 标签纸边框 + 顶部色带，模仿仓库贴在料盒上的规格标签
    d.rectangle([14, 14, W - 14, H - 14], outline=(90, 96, 104), width=3)
    d.rectangle([14, 14, W - 14, 74], fill=(28, 62, 110))
    d.text((34, 28), "备件规格标签", font=load(font_path, font_index, 26), fill=(255, 255, 255))

    label_font = load(font_path, font_index, 20)
    value_font = load(font_path, font_index, 22)
    y = 96
    for name, value in LABEL_ROWS:
        d.text((40, y), name, font=label_font, fill=(96, 104, 116))
        d.text((230, y), value, font=value_font, fill=(22, 28, 36))
        y += 52

    d.text(
        (34, y + 12),
        "注：替代件须满足线径公差 ±0.05 mm、硬度偏差 ±5 Shore A。",
        font=load(font_path, font_index, 15),
        fill=(120, 128, 142),
    )

    img.save(path)


# --------------------------------------------------------------------------- #
# 期望产物清单：与 data/eval/golden_image.jsonl 的 image 字段一一对应
# --------------------------------------------------------------------------- #
FIXTURES: tuple[str, ...] = (
    "alarm_E2041.png",
    "cvd_param_table.png",
    "handwritten_note.png",
    "cvd_alarm_E3061.png",
    "parts_label_oring.png",
)


def main() -> int:
    parser = argparse.ArgumentParser(description="生成图片类评测 fixture")
    parser.add_argument("--check", action="store_true", help="只检查是否齐备，不生成")
    parser.add_argument("--out", type=Path, default=OUT_DIR, help="输出目录")
    args = parser.parse_args()

    out_dir: Path = args.out
    if args.check:
        missing = [name for name in FIXTURES if not (out_dir / name).is_file()]
        for name in FIXTURES:
            mark = "缺失" if name in missing else "存在"
            print(f"  [{mark}] {out_dir / name}")
        if missing:
            print(f"\n缺少 {len(missing)} 张，跑 `.venv/bin/python scripts/make_image_fixtures.py` 生成。")
            return 1
        print(f"\n{len(FIXTURES)} 张 fixture 齐备。")
        return 0

    found = find_font(FONT_CANDIDATES)
    if found is None:
        print("找不到可用的中文字体。请安装任一款后重试，例如：", file=sys.stderr)
        print("  sudo apt install fonts-noto-cjk        # Debian/Ubuntu", file=sys.stderr)
        print("  或确认 WSL 下 /mnt/c/Windows/Fonts 可读", file=sys.stderr)
        print("\n候选路径：", file=sys.stderr)
        for p, _ in FONT_CANDIDATES:
            print(f"  {p}", file=sys.stderr)
        return 1

    font_path, font_index = found
    hand = find_font(HANDWRITING_CANDIDATES) or found
    hand_path, hand_index = hand

    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"字体：{font_path}（手写体：{hand_path}）")
    print(f"输出：{out_dir}")

    render_alarm(out_dir / "alarm_E2041.png", font_path, font_index)
    print("  √ alarm_E2041.png       报警码 E-2041 + 腔体压力异常")
    render_param_table(out_dir / "cvd_param_table.png", font_path, font_index)
    print("  √ cvd_param_table.png   780 ℃ / 300 Pa / 350 sccm")
    render_handwritten_note(out_dir / "handwritten_note.png", hand_path, hand_index)
    print("  √ handwritten_note.png  故意模糊，期望识别失败（负样本）")
    render_cvd_alarm(out_dir / "cvd_alarm_E3061.png", font_path, font_index)
    print("  √ cvd_alarm_E3061.png   报警码 E-3061 + 温度超限")
    render_parts_label(out_dir / "parts_label_oring.png", font_path, font_index)
    print("  √ parts_label_oring.png 备件标签 SP-ETA-0101 / FKM / 320 mm × 5.33 mm")

    print("\n完成。复核：.venv/bin/python scripts/make_image_fixtures.py --check")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
