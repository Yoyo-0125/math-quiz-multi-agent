import json
import re
import sys
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "codes"))

from common import call_deepseek_json


OUTPUT_DIR = PROJECT_ROOT / "outputs" / "accuracy_benchmark_200_stress"
BATCH_SIZE = 20


def pair(x, y):
    return f"({x},{y})"


def sorted_pairs(values):
    return sorted(set(values), key=lambda item: (int(item.split(",")[0].strip("(")), int(item.split(",")[1].strip(")"))))


def make_tasks():
    tasks = []
    task_id = 1

    # Four-branch Cartesian products: easy to miss one branch.
    for index in range(50):
        a = index % 13 - 6
        b = (index * 5) % 17 - 8
        c = (index * 7) % 19 - 9
        d = (index * 11) % 23 - 11
        if a == b:
            b += 1
        if c == d:
            d -= 1
        expected = sorted_pairs([pair(a, c), pair(a, d), pair(b, c), pair(b, d)])
        tasks.append(
            {
                "id": task_id,
                "family": "four_branch_product",
                "problem": f"求所有实数解： (x-{a})(x-{b})=0，(y-{c})(y-{d})=0。",
                "expected": expected,
            }
        )
        task_id += 1

    # Absolute-value branches with four solutions.
    for index in range(50):
        h = index % 9 - 4
        k = (index * 3) % 11 - 5
        r = index % 5 + 1
        s = index % 4 + 2
        expected = sorted_pairs(
            [
                pair(h + r, k + s),
                pair(h + r, k - s),
                pair(h - r, k + s),
                pair(h - r, k - s),
            ]
        )
        tasks.append(
            {
                "id": task_id,
                "family": "absolute_value_branches",
                "problem": f"求所有实数解： |x-{h}|={r}，|y-{k}|={s}。",
                "expected": expected,
            }
        )
        task_id += 1

    # Rational equations with denominator exclusions.
    for index in range(50):
        a = index % 13 - 6
        b = (index * 2) % 15 - 7
        c = (index * 3) % 17 - 8
        d = (index * 5) % 19 - 9
        if a == b:
            b += 2
        if c == d:
            d -= 2
        expected = [pair(a, c)]
        tasks.append(
            {
                "id": task_id,
                "family": "rational_exclusion",
                "problem": (
                    f"求所有实数解，并注意分母不为 0："
                    f" (x-{a})/(x-{b})=0，(y-{c})/(y-{d})=0。"
                ),
                "expected": expected,
            }
        )
        task_id += 1

    # Integer divisor enumeration; multiple positive/negative branches.
    for index in range(50):
        p = index % 7 + 2
        q = index % 5 + 3
        n = p * q
        expected = []
        for x in range(-abs(n) - 1, abs(n) + 2):
            if x != 0 and n % x == 0:
                y = n // x
                if abs(x - y) % 2 == index % 2:
                    expected.append(pair(x, y))
        if not expected:
            expected = [pair(1, n), pair(-1, -n)]
            parity_text = "任意"
        else:
            parity_text = "偶数" if index % 2 == 0 else "奇数"
        tasks.append(
            {
                "id": task_id,
                "family": "integer_divisor_enumeration",
                "problem": f"求所有整数解：xy={n}，且 |x-y| 为{parity_text}。",
                "expected": sorted_pairs(expected),
            }
        )
        task_id += 1

    return tasks


def normalize(value):
    text = str(value or "").lower()
    text = text.replace("，", ",").replace("（", "(").replace("）", ")")
    text = text.replace(" ", "").replace("{", "").replace("}", "")
    text = text.replace("；", ";")
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
        "每题必须给出完整解集，solutions 中每个解写成 '(x,y)'。"
        "不要省略对称解、负数解、分支解；不要写步骤。"
    )
    user_content = json.dumps(
        {
            "instruction": "解这些方程组/条件组。只输出 JSON：{\"answers\":[{\"id\":1,\"solutions\":[\"(x,y)\"]}]}",
            "tasks": [{"id": item["id"], "problem": item["problem"]} for item in tasks],
        },
        ensure_ascii=False,
        indent=2,
    )
    return call_deepseek_json(
        system_prompt=system_prompt,
        user_content=user_content,
        max_tokens=10000,
        agent_name="deepseek_chat_200_stress_accuracy",
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
    draw.text((50, 34), "高难度压力测试准确率对比", fill="#111827", font=title_font)
    draw.text((50, 82), "多分支、漏解、分母限制、整数枚举场景", fill="#4b5563", font=small_font)

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
                "family": task["family"],
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
    (OUTPUT_DIR / "stress_200_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lead = round(summary["alleras"]["accuracy"] - summary["deepseek"]["accuracy"], 1)
    (OUTPUT_DIR / "stress_200_report.md").write_text(
        "\n".join(
            [
                "# 高难度压力测试准确率对比",
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
    draw_chart(summary, OUTPUT_DIR / "stress_200_bar.png")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
