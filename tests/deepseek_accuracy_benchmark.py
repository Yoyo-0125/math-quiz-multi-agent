import json
import re
import sys
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "codes"))

from common import call_deepseek_json


OUTPUT_DIR = PROJECT_ROOT / "outputs" / "accuracy_benchmark"


TASKS = [
    {
        "id": 1,
        "problem": "解方程组：y = x^2 - 5x + 4，x - y = 2。",
        "expected": ["(3+sqrt(3),1+sqrt(3))", "(3-sqrt(3),1-sqrt(3))"],
    },
    {
        "id": 2,
        "problem": "解方程组：x^2+y^2+xy=19，x-y=1。",
        "expected": ["(3,2)", "(-2,-3)"],
    },
    {
        "id": 3,
        "problem": "解方程组：x^2+2xy-3y^2+x-5y-2=0，2x^2+4xy-6y^2-x+5y+2=0。",
        "expected": ["(-1/2,-1/2)", "(3/4,-1/4)"],
    },
    {
        "id": 4,
        "problem": "解方程组：x/y + y/x = 13/6，x+y=5，且 x,y 不为 0。",
        "expected": ["(2,3)", "(3,2)"],
    },
    {
        "id": 5,
        "problem": "解方程组：2x^2+xy-y^2-2x+y=0，x-y=1。",
        "expected": ["(1,0)", "(-1,-2)"],
    },
    {
        "id": 6,
        "problem": "解方程组：x^2-3xy+2y^2+x-y=0，xy-2x-y+2=0。",
        "expected": ["(1,1)", "(2,2)", "(3,2)"],
    },
]


PROJECT_CURRENT_CORRECTNESS = {
    1: True,
    2: True,
    3: False,
    4: True,
    5: False,
    6: False,
}


def normalize_answer(value):
    text = str(value or "").lower()
    text = text.replace("\\sqrt", "sqrt")
    text = text.replace(" ", "")
    text = text.replace("\\frac{1}{2}", "1/2")
    text = text.replace("\\frac{3}{4}", "3/4")
    text = text.replace("\\frac{1}{4}", "1/4")
    text = text.replace("，", ",")
    text = text.replace("（", "(").replace("）", ")")
    text = text.replace("{", "").replace("}", "")
    return text


def canonical_set(values):
    return {normalize_answer(value) for value in values}


def score_solutions(expected, actual):
    expected_set = canonical_set(expected)
    actual_set = canonical_set(actual)
    return expected_set == actual_set


def run_deepseek_chat():
    system_prompt = (
        "你是高中数学解题助手。请只返回合法 JSON，不要 Markdown。"
        "每道题给出完整解集，solutions 中每个解写成字符串 '(x,y)'，"
        "含根号时用 sqrt(3)，分数用 -1/2 这种形式。"
    )
    user_content = json.dumps(
        {
            "instruction": "解下列方程组。只输出 JSON：{\"answers\":[{\"id\":1,\"solutions\":[\"(x,y)\"]}]}",
            "tasks": [{"id": item["id"], "problem": item["problem"]} for item in TASKS],
        },
        ensure_ascii=False,
        indent=2,
    )
    return call_deepseek_json(
        system_prompt=system_prompt,
        user_content=user_content,
        max_tokens=3000,
        agent_name="deepseek_chat_accuracy_benchmark",
        model_name="deepseek-v4-flash",
        response_format_json=True,
    )


def evaluate_deepseek(payload):
    answers = payload.get("answers", []) if isinstance(payload, dict) else []
    by_id = {int(item.get("id")): item for item in answers if str(item.get("id", "")).isdigit()}
    rows = []
    for task in TASKS:
        actual = by_id.get(task["id"], {}).get("solutions", [])
        correct = score_solutions(task["expected"], actual)
        rows.append(
            {
                "id": task["id"],
                "expected": task["expected"],
                "actual": actual,
                "correct": correct,
            }
        )
    return rows


def percent(correct_count, total):
    return round(correct_count * 100 / max(1, total), 1)


def load_font(size):
    candidates = [
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ]
    for path in candidates:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def draw_bar_chart(summary, output_path):
    width, height = 1100, 720
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    title_font = load_font(36)
    label_font = load_font(24)
    small_font = load_font(20)
    draw.text((50, 35), "复杂方程组任务准确率对比", fill="#111827", font=title_font)
    draw.text((50, 82), "样本：6 道复杂方程组；DeepSeek baseline 使用 chat/completions，thinking=disabled", fill="#4b5563", font=small_font)

    chart_left, chart_top = 140, 150
    chart_width, chart_height = 820, 420
    draw.line((chart_left, chart_top, chart_left, chart_top + chart_height), fill="#374151", width=2)
    draw.line((chart_left, chart_top + chart_height, chart_left + chart_width, chart_top + chart_height), fill="#374151", width=2)

    for tick in range(0, 101, 20):
        y = chart_top + chart_height - int(chart_height * tick / 100)
        draw.line((chart_left - 8, y, chart_left + chart_width, y), fill="#e5e7eb", width=1)
        draw.text((70, y - 13), f"{tick}%", fill="#374151", font=small_font)

    labels = ["本项目流程", "DeepSeek chat"]
    colors = ["#2563eb", "#10b981"]
    bar_width = 170
    gap = 190
    start_x = chart_left + 145
    for index, label in enumerate(labels):
        value = summary[label]["accuracy"]
        x0 = start_x + index * (bar_width + gap)
        y0 = chart_top + chart_height - int(chart_height * value / 100)
        x1 = x0 + bar_width
        y1 = chart_top + chart_height
        draw.rectangle((x0, y0, x1, y1), fill=colors[index])
        draw.text((x0 + 30, y0 - 36), f"{value:.1f}%", fill="#111827", font=label_font)
        draw.text((x0 - 8, y1 + 22), label, fill="#111827", font=label_font)
        draw.text(
            (x0 + 15, y1 + 58),
            f"{summary[label]['correct']}/{summary[label]['total']} 正确",
            fill="#4b5563",
            font=small_font,
        )

    note = (
        "判分口径：解集完全一致才计正确；多解、漏解、额外错误解均计错。"
    )
    draw.text((50, 650), note, fill="#4b5563", font=small_font)
    image.save(output_path)


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    deepseek_payload = run_deepseek_chat()
    deepseek_rows = evaluate_deepseek(deepseek_payload)

    project_correct = sum(1 for value in PROJECT_CURRENT_CORRECTNESS.values() if value)
    deepseek_correct = sum(1 for row in deepseek_rows if row["correct"])
    total = len(TASKS)
    summary = {
        "本项目流程": {
            "correct": project_correct,
            "total": total,
            "accuracy": percent(project_correct, total),
            "basis": "outputs/answer_key_final.md 当前项目输出人工/规则核验",
        },
        "DeepSeek chat": {
            "correct": deepseek_correct,
            "total": total,
            "accuracy": percent(deepseek_correct, total),
            "basis": "deepseek-v4-flash chat/completions direct answer",
        },
    }
    report = {
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "tasks": TASKS,
        "project_current_correctness": PROJECT_CURRENT_CORRECTNESS,
        "deepseek_raw": deepseek_payload,
        "deepseek_evaluation": deepseek_rows,
        "summary": summary,
    }
    (OUTPUT_DIR / "accuracy_benchmark_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    markdown_lines = [
        "# 复杂任务准确率对比",
        "",
        f"- 本项目流程：{project_correct}/{total}，{summary['本项目流程']['accuracy']}%",
        f"- DeepSeek chat：{deepseek_correct}/{total}，{summary['DeepSeek chat']['accuracy']}%",
        "",
        "## DeepSeek chat 明细",
        "",
    ]
    for row in deepseek_rows:
        markdown_lines.append(
            f"{row['id']}. {'正确' if row['correct'] else '错误'}；"
            f"标准：{row['expected']}；DeepSeek：{row['actual']}"
        )
    (OUTPUT_DIR / "accuracy_benchmark_report.md").write_text(
        "\n".join(markdown_lines) + "\n",
        encoding="utf-8",
    )
    draw_bar_chart(summary, OUTPUT_DIR / "accuracy_benchmark_bar.png")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
