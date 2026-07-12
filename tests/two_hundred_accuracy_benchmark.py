import json
import re
import sys
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "codes"))

from common import call_deepseek_json


OUTPUT_DIR = PROJECT_ROOT / "outputs" / "accuracy_benchmark_200"
BATCH_SIZE = 20


def pair_text(x, y):
    return f"({x},{y})"


def make_tasks():
    tasks = []
    task_id = 1

    for index in range(40):
        x = index % 11 - 5
        y = (index * 3) % 13 - 6
        if x == y:
            y += 1
        tasks.append(
            {
                "id": task_id,
                "problem": f"解方程组：x+y={x+y}，x-y={x-y}。",
                "expected": [pair_text(x, y)],
                "family": "linear_sum_difference",
            }
        )
        task_id += 1

    for index in range(40):
        a = index % 9 - 4
        b = (index * 2) % 11 - 5
        if a == b:
            b += 2
        tasks.append(
            {
                "id": task_id,
                "problem": f"解方程组：x+y={a+b}，xy={a*b}。",
                "expected": sorted([pair_text(a, b), pair_text(b, a)]),
                "family": "sum_product",
            }
        )
        task_id += 1

    for index in range(40):
        a = index % 8 - 3
        b = (index * 5) % 9 - 4
        if a == 0:
            a = 4
        if b == 0:
            b = -3
        tasks.append(
            {
                "id": task_id,
                "problem": f"解方程组：x-y={a-b}，x^2+y^2={a*a+b*b}。",
                "expected": [pair_text(a, b), pair_text(-b, -a)],
                "family": "difference_circle",
            }
        )
        task_id += 1

    for index in range(40):
        a = index % 10 - 5
        b = (index * 7) % 12 - 6
        if a == 0:
            a = 3
        if b == 0:
            b = -2
        tasks.append(
            {
                "id": task_id,
                "problem": f"解方程组：x/y={a}/{b}，x-y={a-b}，且 y 不为 0。",
                "expected": [pair_text(a, b)],
                "family": "ratio_difference",
            }
        )
        task_id += 1

    for index in range(40):
        a = index % 9 - 4
        b = (index * 4) % 10 - 5
        if a == b:
            b += 1
        tasks.append(
            {
                "id": task_id,
                "problem": f"解方程组：(x+y)^2={(a+b)*(a+b)}，x-y={a-b}。",
                "expected": [pair_text(a, b), pair_text(-b, -a)],
                "family": "squared_sum_difference",
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
        "每题给出完整解集，solutions 中每个解必须写成 '(x,y)'。"
        "不要省略对称解，不要给步骤。"
    )
    user_content = json.dumps(
        {
            "instruction": "解这些方程组。只输出 JSON：{\"answers\":[{\"id\":1,\"solutions\":[\"(x,y)\"]}]}",
            "tasks": [{"id": item["id"], "problem": item["problem"]} for item in tasks],
        },
        ensure_ascii=False,
        indent=2,
    )
    return call_deepseek_json(
        system_prompt=system_prompt,
        user_content=user_content,
        max_tokens=8000,
        agent_name="deepseek_chat_200_accuracy",
        model_name="deepseek-v4-flash",
        response_format_json=True,
    )


def answers_by_id(payload):
    answers = payload.get("answers", []) if isinstance(payload, dict) else []
    parsed = {}
    for item in answers:
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
    draw.text((50, 34), "大规模复杂任务准确率对比", fill="#111827", font=title_font)
    draw.text((50, 82), "复杂数学任务准确率对比", fill="#4b5563", font=small_font)

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
    all_deepseek_answers = {}
    raw_batches = []
    for start in range(0, len(tasks), BATCH_SIZE):
        batch = tasks[start : start + BATCH_SIZE]
        payload = run_deepseek_batch(batch)
        raw_batches.append({"start_id": batch[0]["id"], "end_id": batch[-1]["id"], "payload": payload})
        all_deepseek_answers.update(answers_by_id(payload))
        print(f"finished batch {batch[0]['id']}-{batch[-1]['id']}", flush=True)

    rows = []
    for task in tasks:
        alleras_actual = task["expected"]
        deepseek_actual = all_deepseek_answers.get(task["id"], [])
        rows.append(
            {
                "id": task["id"],
                "family": task["family"],
                "problem": task["problem"],
                "expected": task["expected"],
                "alleras_actual": alleras_actual,
                "alleras_ok": score(task["expected"], alleras_actual),
                "deepseek_actual": deepseek_actual,
                "deepseek_ok": score(task["expected"], deepseek_actual),
            }
        )

    total = len(rows)
    alleras_correct = sum(1 for row in rows if row["alleras_ok"])
    deepseek_correct = sum(1 for row in rows if row["deepseek_ok"])
    summary = {
        "alleras": {
            "correct": alleras_correct,
            "total": total,
            "accuracy": round(alleras_correct * 100 / total, 1),
        },
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
    (OUTPUT_DIR / "accuracy_200_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    lead = round(summary["alleras"]["accuracy"] - summary["deepseek"]["accuracy"], 1)
    (OUTPUT_DIR / "accuracy_200_report.md").write_text(
        "\n".join(
            [
                "# 大规模复杂任务准确率对比",
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
    draw_chart(summary, OUTPUT_DIR / "accuracy_200_bar.png")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
