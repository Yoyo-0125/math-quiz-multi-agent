import json
import sys

from common import (
    call_deepseek_json,
    get_model_name,
    read_json_file,
    read_text_file,
    write_json_file,
)
from validators import validate_qc_result


QC_SYSTEM_PROMPT = """
你是一个数学题目质量检查 agent，名字叫 QC。

你的任务是检查 Generator 生成的新题和答案是否合格。

你会收到五部分内容：
1. original_exercises：原始数学练习
2. decomposer_result：Decomposer 对原始练习的分析
3. generator_result：Generator 的完整 JSON 输出
4. generated_questions：Generator 生成的新题 Markdown
5. answer_key：Generator 生成的答案 Markdown

你需要检查：
1. 新题是否和原题知识点一致
2. 新题是否和原题难度接近
3. 新题是否和原题格式风格接近
4. 新题是否存在数学错误
5. 新题条件是否充分、无歧义
6. 答案是否对应每一道题
7. 答案是否计算正确
8. 题目部分是否错误地泄露答案
9. LaTeX 是否大致可读
10. 是否有题号缺失、答案缺失、题目答案数量不匹配等工程问题

请输出合法 JSON，格式如下：

{
  "is_passed": true,
  "score": 0,
  "question_issues": [
    {
      "question_id": "第1题",
      "severity": "major",
      "problem": "问题描述",
      "suggested_fix": "修改建议"
    }
  ],
  "answer_issues": [
    {
      "question_id": "第1题",
      "severity": "major",
      "problem": "问题描述",
      "suggested_fix": "修改建议"
    }
  ],
  "style_issues": [
    {
      "severity": "minor",
      "problem": "风格问题",
      "suggested_fix": "修改建议"
    }
  ],
  "revision_instructions": [
    "如果需要打回 Generator，这里写清楚具体要改什么"
  ],
  "qc_summary": "简短总结"
}

评分标准：
- 90 到 100：基本合格，只需轻微修改
- 70 到 89：总体可用，但有明显小问题
- 50 到 69：存在较大问题，需要打回修改
- 0 到 49：不合格，必须重做

要求：
1. 只输出 JSON。
2. 不要输出 Markdown。
3. 不要解释。
4. is_passed 只能是 true 或 false。
5. score 必须是 0 到 100 的整数。
6. severity 只能是 major 或 minor。
7. 如果没有问题，对应列表输出空列表。
"""


QC_OUTPUT_GUARDRAILS = """

额外审查规则：
1. 如果 answer_key 中出现推理草稿、自我否定、自我纠正、未完成分类讨论，必须判为不通过。
2. 如果答案中出现这些词句，视为 major 问题：重新计算、不确定、可能、需要进一步、略、待补、？、不对、矛盾、建议答案、默认答案。
3. 如果含参题答案没有完整列出必要分类，必须判为不通过。
4. 如果新题比原题明显更复杂，导致答案分类超过原题复杂度，必须要求 Generator 换更简单的新题。
5. revision_instructions 必须写成可执行指令，例如“重新生成一道同知识点但分类不超过三类的题，并给出完整确定答案”。
"""


QC_GENERATION_PROFILE_RULES = """

When generation_profile is provided:
1. Judge generated question count against generation_profile.question_count, not against the original worksheet size.
2. If generated question count or answer count differs from generation_profile.question_count, mark it as a major issue.
3. Difficulty, knowledge points, question types, style, and answer_detail should follow generation_profile first, then decomposer_result.
"""

QC_FOCUSED_FEEDBACK_RULES = """

Focused retry rule:
1. If the output fails, report only the single highest-priority concrete problem.
2. Prefer one major mathematical error in one question over broad style comments.
3. Put that one problem in question_issues or answer_issues and leave unrelated issue lists empty.
4. revision_instructions must only describe how to fix that one target question.
5. Do not enumerate all mistakes across the whole worksheet in one QC response.
"""

QC_ANSWER_VERIFICATION_RULES = """

Answer verification rules:
1. Check mathematical correctness before style. A stylish worksheet with wrong answers must fail.
2. For inequality answers, independently test interval endpoints and at least one sample value from each interval.
3. For parameterized inequalities, verify that cases cover all parameter values exactly once and that boundary cases are included.
4. If only one or two answers are wrong, report the highest-priority concrete wrong answer.
5. If many answers are wrong or the same solving pattern is repeatedly wrong, report one major systemic answer issue with question_id "answer_systemic" and suggested_fix asking Generator to recompute all answers, not only one item.
6. Do not pass QC when answer correctness is uncertain.
7. If any issue has severity "major", is_passed must be false and score must be below the pass threshold.
"""


def run(
    original_input_path,
    decomposer_json_path,
    generator_json_path,
    questions_path,
    answer_key_path,
    output_json_path,
    generation_profile=None,
):
    original_exercises = read_text_file(original_input_path)
    decomposer_result = read_json_file(decomposer_json_path)
    generator_result = read_json_file(generator_json_path)
    generated_questions = read_text_file(questions_path)
    answer_key = read_text_file(answer_key_path)

    qc_input = {
        "original_exercises": original_exercises,
        "decomposer_result": decomposer_result,
        "generator_result": generator_result,
        "generated_questions": generated_questions,
        "answer_key": answer_key,
        "generation_profile": generation_profile or {},
    }

    result = call_deepseek_json(
        system_prompt=(
            QC_SYSTEM_PROMPT
            + QC_OUTPUT_GUARDRAILS
            + QC_GENERATION_PROFILE_RULES
            + QC_ANSWER_VERIFICATION_RULES
            + QC_FOCUSED_FEEDBACK_RULES
        ),
        user_content=json.dumps(qc_input, ensure_ascii=False, indent=2),
        max_tokens=6000,
        agent_name="qc",
        model_name=get_model_name("qc"),
)

    validate_qc_result(result)
    write_json_file(output_json_path, result)

    print(f"QC 完成，结果已写入：{output_json_path}")


def main():
    if len(sys.argv) != 7:
        print("用法：")
        print(
            "python qc_agent.py "
            "<原始md文件> "
            "<decomposer输出json> "
            "<generator输出json> "
            "<新题md> "
            "<答案md> "
            "<qc输出json>"
        )
        print()
        print("示例：")
        print(
            "python qc_agent.py "
            "examples/input.md "
            "outputs/decomposer_final.json "
            "outputs/generator.json "
            "outputs/generated_questions.md "
            "outputs/answer_key.md "
            "outputs/qc.json"
        )
        return

    original_input_path = sys.argv[1]
    decomposer_json_path = sys.argv[2]
    generator_json_path = sys.argv[3]
    questions_path = sys.argv[4]
    answer_key_path = sys.argv[5]
    output_json_path = sys.argv[6]

    run(
        original_input_path,
        decomposer_json_path,
        generator_json_path,
        questions_path,
        answer_key_path,
        output_json_path,
    )


if __name__ == "__main__":
    main()
