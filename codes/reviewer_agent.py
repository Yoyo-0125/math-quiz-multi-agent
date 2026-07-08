import json
import sys

from common import (
    call_deepseek_json,
    get_model_name,
    read_json_file,
    read_text_file,
    write_json_file,
)
from validators import validate_reviewer_result


REVIEWER_SYSTEM_PROMPT = """
你是一个数学练习分析审核 agent，名字叫 Reviewer。

你的任务是审核 Decomposer 对数学练习的分析是否准确、完整、合理。

你会收到两部分内容：
1. original_exercises：原始数学练习
2. decomposer_result：Decomposer 输出的分析 JSON

请检查：
1. 主要知识点是否准确
2. 次级知识点是否有遗漏或误判
3. 年级判断是否合理
4. 题型判断是否准确
5. 难度分布是否合理
6. 格式结构是否描述清楚
7. 风格特征是否足够支持后续仿写
8. LaTeX 使用情况是否判断正确
9. generation_constraints 是否足够具体
10. uncertain_points 是否诚实标出不确定处

请输出合法 JSON，格式如下：

{
  "is_valid": true,
  "score": 0,
  "major_issues": ["严重问题1", "严重问题2"],
  "minor_issues": ["小问题1", "小问题2"],
  "missing_points": ["遗漏点1", "遗漏点2"],
  "suggested_corrections": {
    "topic": "如果需要修正，写修正后的 topic；不需要则写 null",
    "subtopics": ["如果需要修正，写修正后的 subtopics；不需要则写 null"],
    "grade_level": "如果需要修正，写修正后的 grade_level；不需要则写 null",
    "question_types": ["如果需要修正，写修正后的 question_types；不需要则写 null"]
  },
  "review_summary": "简短总结审核结果"
}

评分标准：
- 90 到 100：分析基本准确，只需轻微修改
- 70 到 89：总体可用，但有一些遗漏或不够具体
- 50 到 69：有明显问题，需要修改后再使用
- 0 到 49：分析不可靠

要求：
1. 只输出 JSON。
2. 不要输出 Markdown。
3. 不要解释。
4. is_valid 只能是 true 或 false。
5. score 必须是 0 到 100 的整数。
6. 所有字符串内容中禁止使用英文双引号 "。如果要引用题型、概念或原文，用中文书名号《》或中文引号“”。
"""


def run(original_input_path, decomposer_json_path, output_path):
    original_exercises = read_text_file(original_input_path)
    decomposer_result = read_json_file(decomposer_json_path)

    reviewer_input = {
        "original_exercises": original_exercises,
        "decomposer_result": decomposer_result,
    }

    result = call_deepseek_json(
        system_prompt=REVIEWER_SYSTEM_PROMPT,
        user_content=json.dumps(reviewer_input, ensure_ascii=False, indent=2),
        max_tokens=2000,
        agent_name="reviewer",
        model_name=get_model_name("reviewer"),
)

    validate_reviewer_result(result)
    write_json_file(output_path, result)

    print(f"Reviewer 完成，结果已写入：{output_path}")


def main():
    if len(sys.argv) != 4:
        print("用法：python reviewer_agent.py <原始md文件> <decomposer输出json> <reviewer输出json>")
        print("示例：python reviewer_agent.py examples/input.md outputs/decomposer.json outputs/reviewer.json")
        return

    original_input_path = sys.argv[1]
    decomposer_json_path = sys.argv[2]
    output_path = sys.argv[3]

    run(original_input_path, decomposer_json_path, output_path)


if __name__ == "__main__":
    main()
