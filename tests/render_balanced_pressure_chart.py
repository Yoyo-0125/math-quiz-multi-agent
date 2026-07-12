import json
import os
import shutil
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs" / "accuracy_benchmark_balanced"


def load_font(size):
    for path in [
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ]:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def main():
    report = json.loads((OUT / "balanced_pressure_report.json").read_text(encoding="utf-8"))
    summary = report["summary"]
    lead = round(summary["alleras"]["accuracy"] - summary["deepseek"]["accuracy"], 1)

    image = Image.new("RGB", (1180, 740), "white")
    draw = ImageDraw.Draw(image)
    title_font = load_font(34)
    label_font = load_font(24)
    small_font = load_font(19)

    draw.text((50, 34), "综合压力测试准确率对比", fill="#111827", font=title_font)
    draw.text((50, 82), "混合评测：复杂数学、多分支推理与组合枚举", fill="#4b5563", font=small_font)

    left, top, width, height = 140, 150, 860, 420
    draw.line((left, top, left, top + height), fill="#374151", width=2)
    draw.line((left, top + height, left + width, top + height), fill="#374151", width=2)
    for tick in range(0, 101, 20):
        y = top + height - int(height * tick / 100)
        draw.line((left - 8, y, left + width, y), fill="#e5e7eb", width=1)
        draw.text((70, y - 12), f"{tick}%", fill="#374151", font=small_font)

    bars = [
        ("Alleras 多智能体", summary["alleras"], "#2563eb"),
        ("DeepSeek chat", summary["deepseek"], "#10b981"),
    ]
    bar_width = 185
    start_x = left + 145
    gap = 230
    for index, (name, item, color) in enumerate(bars):
        value = item["accuracy"]
        x0 = start_x + index * (bar_width + gap)
        y0 = top + height - int(height * value / 100)
        x1 = x0 + bar_width
        y1 = top + height
        draw.rectangle((x0, y0, x1, y1), fill=color)
        draw.text((x0 + 28, y0 - 36), f"{value:.1f}%", fill="#111827", font=label_font)
        draw.text((x0 - 15, y1 + 24), name, fill="#111827", font=label_font)
        draw.text((x0 + 48, y1 + 60), "准确率", fill="#4b5563", font=small_font)

    draw.text(
        (50, 650),
        f"结论：Alleras 准确率领先 DeepSeek chat {lead} 个百分点。",
        fill="#111827",
        font=small_font,
    )

    output = OUT / "balanced_pressure_bar.png"
    image.save(output)
    shutil.copy2(output, OUT / "Alleras_balanced_pressure_chart.png")
    shutil.copy2(output, Path(os.environ["USERPROFILE"]) / "Desktop" / "Alleras_balanced_pressure_chart.png")
    print(output)


if __name__ == "__main__":
    main()
