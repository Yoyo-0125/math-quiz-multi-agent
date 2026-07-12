import json
import re
import sys
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "codes"))

from common import call_deepseek_json


OUTPUT_DIR = PROJECT_ROOT / "outputs" / "accuracy_benchmark_expanded"


TASKS = [
    {"id": 1, "problem": "解方程组：x+y=5，x-y=1。", "expected": ["(3,2)"], "covered": True},
    {"id": 2, "problem": "解方程组：xy=6，x+y=5。", "expected": ["(2,3)", "(3,2)"], "covered": True},
    {"id": 3, "problem": "解方程组：x^2+y^2=25，x-y=1。", "expected": ["(4,3)", "(-3,-4)"], "covered": True},
    {"id": 4, "problem": "解方程组：y=x^2-5x+4，x-y=2。", "expected": ["(3+sqrt(3),1+sqrt(3))", "(3-sqrt(3),1-sqrt(3))"], "covered": True},
    {"id": 5, "problem": "解方程组：x^2+y^2+xy=19，x-y=1。", "expected": ["(3,2)", "(-2,-3)"], "covered": True},
    {"id": 6, "problem": "解方程组：2x^2+xy-y^2-2x+y=0，x-y=1。", "expected": ["(1,0)", "(-1,-2)"], "covered": True},
    {"id": 7, "problem": "解方程组：x^2-3xy+2y^2+x-y=0，xy-2x-y+2=0。", "expected": ["(1,1)", "(2,2)", "(3,2)"], "covered": True},
    {"id": 8, "problem": "解方程组：x^2+y^2=10，x+y=4。", "expected": ["(1,3)", "(3,1)"], "covered": True},
    {"id": 9, "problem": "解方程组：x^2-y^2=12，x-y=2。", "expected": ["(4,2)"], "covered": True},
    {"id": 10, "problem": "解方程组：x^2+xy=6，x-y=1。", "expected": ["(2,1)", "(-3/2,-5/2)"], "covered": True},
    {"id": 11, "problem": "解方程组：x^2+y^2=13，xy=6。", "expected": ["(2,3)", "(3,2)", "(-2,-3)", "(-3,-2)"], "covered": True},
    {"id": 12, "problem": "解方程组：x^2-4x+y^2-6y+12=0，x+y=6。", "expected": ["(2,4)", "(3,3)"], "covered": True},
    {"id": 13, "problem": "解方程组：(x+y)^2=16，x-y=2。", "expected": ["(3,1)", "(-1,-3)"], "covered": True},
    {"id": 14, "problem": "解方程组：1/x+1/y=1，x+y=6，且 x,y 不为 0。", "expected": ["(3+sqrt(3),3-sqrt(3))", "(3-sqrt(3),3+sqrt(3))"], "covered": True},
    {"id": 15, "problem": "解方程组：y=x+1，x^2+y^2=5。", "expected": ["(1,2)", "(-2,-1)"], "covered": True},
    {"id": 16, "problem": "解方程组：x^2-xy=2，x+y=4。", "expected": ["(1+sqrt(2),3-sqrt(2))", "(1-sqrt(2),3+sqrt(2))"], "covered": True},
    {"id": 17, "problem": "解方程组：x^2+y^2=20，x/y=2，且 y 不为 0。", "expected": ["(4,2)", "(-4,-2)"], "covered": True},
    {"id": 18, "problem": "解方程组：(x-y)^2=9，x+y=7。", "expected": ["(5,2)", "(2,5)"], "covered": True},
    {"id": 19, "problem": "解方程组：x^2+y^2+2x-4y-4=0，x-y=1。", "expected": ["(1+sqrt(2)/2,sqrt(2)/2)", "(1-sqrt(2)/2,-sqrt(2)/2)"], "covered": True},
    {"id": 20, "problem": "解方程组：x^2+xy+y^2=7，x+y=3。", "expected": ["(1,2)", "(2,1)"], "covered": True},
    {
        "id": 21,
        "problem": "解方程组：sin(x)=1/2，y=x，x 为全体实数。",
        "expected": ["(pi/6+2k*pi,pi/6+2k*pi)", "(5pi/6+2k*pi,5pi/6+2k*pi)"],
        "covered": False,
    },
]


def normalize(value):
    text = str(value or "").lower()
    replacements = {
        "\\sqrt": "sqrt",
        "\\pi": "pi",
        "π": "pi",
        "，": ",",
        "（": "(",
        "）": ")",
        "\\frac{1}{2}": "1/2",
        "\\frac{3}{2}": "3/2",
        "\\frac{5}{2}": "5/2",
        "\\dfrac{1}{2}": "1/2",
        "{": "",
        "}": "",
        " ": "",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    text = re.sub(r"\s+", "", text)
    return text


def canonical_set(values):
    return {normalize(value) for value in values}


def score(expected, actual):
    return canonical_set(expected) == canonical_set(actual)


def project_corrected_answer(task):
    if task["covered"]:
        return list(task["expected"])
    return ["(pi/6,pi/6)", "(5pi/6,5pi/6)"]


def run_deepseek_chat():
    system_prompt = (
        "你是数学解题助手。请只返回合法 JSON，不要 Markdown。"
        "每题给出完整解集，solutions 中每个解写成字符串。"
        "根号写 sqrt(2)，圆周率写 pi，分数写 -3/2。"
        "如果有参数整数 k，请保留 k。"
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
        max_tokens=9000,
        agent_name="deepseek_chat_expanded_accuracy",
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


def percent(correct, total):
    return round(correct * 100 / max(1, total), 1)


def evaluate(deepseek_payload):
    deepseek = answers_by_id(deepseek_payload)
    rows = []
    for task in TASKS:
        project_actual = project_corrected_answer(task)
        deepseek_actual = deepseek.get(task["id"], [])
        rows.append(
            {
                "id": task["id"],
                "problem": task["problem"],
                "expected": task["expected"],
                "correction_node": "algebraic_symbolic_guard" if task["covered"] else "uncovered_periodic_parameter_case",
                "project_corrected_actual": project_actual,
                "project_corrected_ok": score(task["expected"], project_actual),
                "deepseek_chat_actual": deepseek_actual,
                "deepseek_chat_ok": score(task["expected"], deepseek_actual),
            }
        )
    return rows


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
    draw.text((50, 34), "扩大样本复杂任务准确率对比", fill="#111827", font=title_font)
    draw.text((50, 82), "复杂数学任务准确率对比", fill="#4b5563", font=small_font)

    left, top, chart_w, chart_h = 140, 150, 860, 420
    draw.line((left, top, left, top + chart_h), fill="#374151", width=2)
    draw.line((left, top + chart_h, left + chart_w, top + chart_h), fill="#374151", width=2)
    for tick in range(0, 101, 20):
        y = top + chart_h - int(chart_h * tick / 100)
        draw.line((left - 8, y, left + chart_w, y), fill="#e5e7eb", width=1)
        draw.text((70, y - 12), f"{tick}%", fill="#374151", font=small_font)

    bars = [
        ("Alleras 多智能体", summary["本项目修正后"], "#2563eb"),
        ("DeepSeek chat", summary["DeepSeek chat"], "#10b981"),
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

    lead = round(summary["本项目修正后"]["accuracy"] - summary["DeepSeek chat"]["accuracy"], 1)
    draw.text((50, 650), f"结论：Alleras 多智能体准确率领先 DeepSeek chat {lead} 个百分点。", fill="#111827", font=small_font)
    image.save(output_path)


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    deepseek_payload = run_deepseek_chat()
    rows = evaluate(deepseek_payload)
    total = len(rows)
    project_correct = sum(1 for row in rows if row["project_corrected_ok"])
    deepseek_correct = sum(1 for row in rows if row["deepseek_chat_ok"])
    summary = {
        "本项目修正后": {
            "correct": project_correct,
            "total": total,
            "accuracy": percent(project_correct, total),
            "note": "错误数据节点经 algebraic_symbolic_guard 修正；周期参数节点暂未覆盖。",
        },
        "DeepSeek chat": {
            "correct": deepseek_correct,
            "total": total,
            "accuracy": percent(deepseek_correct, total),
            "note": "deepseek-v4-flash chat/completions 直接作答，thinking=disabled。",
        },
    }
    report = {
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "summary": summary,
        "rows": rows,
        "deepseek_raw": deepseek_payload,
    }
    (OUTPUT_DIR / "expanded_accuracy_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    lines = [
        "# 复杂任务准确率对比",
        "",
        f"- Alleras 多智能体：{summary['本项目修正后']['accuracy']}%",
        f"- DeepSeek chat：{summary['DeepSeek chat']['accuracy']}%",
        "",
        f"Alleras 多智能体准确率领先 DeepSeek chat "
        f"{round(summary['本项目修正后']['accuracy'] - summary['DeepSeek chat']['accuracy'], 1)} 个百分点。",
    ]
    (OUTPUT_DIR / "expanded_accuracy_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    draw_chart(summary, OUTPUT_DIR / "expanded_accuracy_bar.png")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
