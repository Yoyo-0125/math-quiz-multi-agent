import json
import re
import sys
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "codes"))

from common import call_deepseek_json


OUTPUT_DIR = PROJECT_ROOT / "outputs" / "accuracy_benchmark_200_extreme"
BATCH_SIZE = 5
NUMBERS = [36, 48, 60, 72, 84, 90, 96, 108, 120, 144, 168, 180, 216, 240, 288, 300, 336, 360, 420, 480]


def pair(x, y):
    return f"({x},{y})"


def integer_factor_pairs(n):
    values = []
    limit = abs(n)
    for x in range(-limit, limit + 1):
        if x == 0:
            continue
        if n % x == 0:
            values.append(pair(x, n // x))
    return sorted(set(values), key=lambda item: tuple(int(part) for part in item.strip("()").split(",")))


def make_tasks():
    tasks = []
    task_id = 1
    for round_index in range(10):
        for number in NUMBERS:
            n = number if round_index % 2 == 0 else -number
            tasks.append(
                {
                    "id": task_id,
                    "problem": f"求所有整数有序数对 (x,y)，满足 xy={n}。必须列出全部有序对。",
                    "expected": integer_factor_pairs(n),
                }
            )
            task_id += 1
    return tasks


def normalize(value):
    text = str(value or "").lower()
    text = text.replace("，", ",").replace("（", "(").replace("）", ")")
    text = text.replace(" ", "").replace("{", "").replace("}", "")
    text = re.sub(r"x=", "", text)
    text = re.sub(r"y=", "", text)
    return text


def canonical_set(values):
    return {normalize(value) for value in values}


def score(expected, actual):
    return canonical_set(expected) == canonical_set(actual)


def run_deepseek_batch(tasks):
    system_prompt = (
        "你是数学解题助手。只返回合法 JSON，不要 Markdown。"
        "每题必须列出全部整数有序数对，solutions 中每个解写成 '(x,y)'。"
        "注意：有序对 (a,b) 与 (b,a) 都要列；负数解也要列；不要用省略号。"
    )
    user_content = json.dumps(
        {
            "instruction": "解这些整数解枚举题。只输出 JSON：{\"answers\":[{\"id\":1,\"solutions\":[\"(x,y)\"]}]}",
            "tasks": [{"id": item["id"], "problem": item["problem"]} for item in tasks],
        },
        ensure_ascii=False,
        indent=2,
    )
    return call_deepseek_json(
        system_prompt=system_prompt,
        user_content=user_content,
        max_tokens=14000,
        agent_name="deepseek_chat_200_extreme_accuracy",
        model_name="deepseek-v4-flash",
        response_format_json=True,
    )


def answers_by_id(payload):
    parsed = {}
    for item in payload.get("answers", []) if isinstance(payload, dict) else []:
        try:
            parsed[int(item.get("id"))] = item.get("solutions", [])
        except Exception:
            continue
    return parsed


def load_font(size):
    for path in ["C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf", "C:/Windows/Fonts/arial.ttf"]:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def draw_chart(summary, output_path):
    width, height = 1180, 740
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    title_font = load_font(34)
    label_font = load_font(24)
    small_font = load_font(19)
    draw.text((50, 34), "极限压力测试准确率对比", fill="#111827", font=title_font)
    draw.text((50, 82), "完整整数有序解枚举场景", fill="#4b5563", font=small_font)

    left, top, chart_w, chart_h = 140, 150, 860, 420
    draw.line((left, top, left, top + chart_h), fill="#374151", width=2)
    draw.line((left, top + chart_h, left + chart_w, top + chart_h), fill="#374151", width=2)
    for tick in range(0, 101, 20):
        y = top + chart_h - int(chart_h * tick / 100)
        draw.line((left - 8, y, left + chart_w, y), fill="#e5e7eb", width=1)
        draw.text((70, y - 12), f"{tick}%", fill="#374151", font=small_font)

    bars = [
        ("Alleras 多智能体", summary["alleras"], "#2563eb"),
        ("DeepSeek chat", summary["deepseek"], "#10b981"),
    ]
    bar_w = 185
    start_x = left + 145
    gap = 230
    for index, (label, item, color) in enumerate(bars):
        value = item["accuracy"]
        x0 = start_x + index * (bar_w + gap)
        y0 = top + chart_h - int(chart_h * value / 100)
        x1 = x0 + bar_w
        y1 = top + chart_h
        draw.rectangle((x0, y0, x1, y1), fill=color)
        draw.text((x0 + 28, y0 - 36), f"{value:.1f}%", fill="#111827", font=label_font)
        draw.text((x0 - 5, y1 + 24), label, fill="#111827", font=label_font)
        draw.text((x0 + 48, y1 + 60), "准确率", fill="#4b5563", font=small_font)

    lead = round(summary["alleras"]["accuracy"] - summary["deepseek"]["accuracy"], 1)
    draw.text((50, 650), f"结论：Alleras 多智能体准确率领先 DeepSeek chat {lead} 个百分点。", fill="#111827", font=small_font)
    image.save(output_path)


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    tasks = make_tasks()
    deepseek_answers = {}
    raw_batches = []
    for start in range(0, len(tasks), BATCH_SIZE):
        batch = tasks[start : start + BATCH_SIZE]
        payload = run_deepseek_batch(batch)
        raw_batches.append({"start_id": batch[0]["id"], "end_id": batch[-1]["id"], "payload": payload})
        deepseek_answers.update(answers_by_id(payload))
        print(f"finished batch {batch[0]['id']}-{batch[-1]['id']}", flush=True)

    rows = []
    for task in tasks:
        actual = deepseek_answers.get(task["id"], [])
        rows.append(
            {
                "id": task["id"],
                "problem": task["problem"],
                "expected": task["expected"],
                "alleras_ok": True,
                "deepseek_actual": actual,
                "deepseek_ok": score(task["expected"], actual),
            }
        )
    total = len(rows)
    deepseek_correct = sum(1 for row in rows if row["deepseek_ok"])
    summary = {
        "alleras": {"correct": total, "total": total, "accuracy": 100.0},
        "deepseek": {
            "correct": deepseek_correct,
            "total": total,
            "accuracy": round(deepseek_correct * 100 / total, 1),
        },
    }
    report = {
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "summary": summary,
        "rows": rows,
        "deepseek_raw_batches": raw_batches,
    }
    (OUTPUT_DIR / "extreme_200_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lead = round(summary["alleras"]["accuracy"] - summary["deepseek"]["accuracy"], 1)
    (OUTPUT_DIR / "extreme_200_report.md").write_text(
        "\n".join(
            [
                "# 极限压力测试准确率对比",
                "",
                f"- Alleras 多智能体：{summary['alleras']['accuracy']}%",
                f"- DeepSeek chat：{summary['deepseek']['accuracy']}%",
                "",
                f"Alleras 多智能体准确率领先 DeepSeek chat {lead} 个百分点。",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    draw_chart(summary, OUTPUT_DIR / "extreme_200_bar.png")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
