import json
import re
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


QC_ORIGINALITY_RULES = """

Originality rules:
1. Similarity to the source is required at the knowledge-point level, not at the wording/formula-copy level.
2. Use originality_report.high_similarity_pairs as hard evidence. If it is not empty, report the highest-similarity pair as a major question issue.
3. A generated item should fail QC if it mostly copies a source item's sentence pattern or formula skeleton and only changes numbers, variable names, or inequality direction.
4. The suggested_fix should ask Generator to rewrite the flagged item by changing wording and formula surface while preserving the same knowledge point and difficulty.
"""


TASK_VERBS = [
    "求",
    "解",
    "讨论",
    "化简",
    "计算",
    "证明",
    "判断",
    "写出",
    "指出",
    "确定",
    "因式分解",
]


def add_major_issue(result, issue_type, question_id, problem, suggested_fix, max_score=84):
    issue = {
        "question_id": str(question_id),
        "severity": "major",
        "problem": problem,
        "suggested_fix": suggested_fix,
    }
    key = "answer_issues" if issue_type == "answer" else "question_issues"
    result.setdefault(key, [])
    if isinstance(result[key], list):
        existing = [
            item
            for item in result[key]
            if isinstance(item, dict)
            and item.get("question_id") == issue["question_id"]
            and item.get("problem") == issue["problem"]
        ]
        if not existing:
            result[key].insert(0, issue)
    result["is_passed"] = False
    try:
        result["score"] = min(int(result.get("score", 0)), max_score)
    except (TypeError, ValueError):
        result["score"] = max_score
    instructions = result.setdefault("revision_instructions", [])
    if isinstance(instructions, list) and suggested_fix not in instructions:
        instructions.insert(0, suggested_fix)
    return result


def strip_math_and_markup(text):
    text = re.sub(r"\$\$.*?\$\$", "", text or "", flags=re.S)
    text = re.sub(r"\$.*?\$", "", text, flags=re.S)
    text = re.sub(r"\\\(.+?\\\)", "", text, flags=re.S)
    text = re.sub(r"\\\[.+?\\\]", "", text, flags=re.S)
    text = re.sub(r"^\s*\d+[.)]\s*", "", text)
    text = re.sub(r"[*_`#>\-]", "", text)
    return text.strip()


def question_has_task(question_text):
    visible_text = strip_math_and_markup(question_text)
    if any(verb in visible_text for verb in TASK_VERBS):
        return True
    lowered = visible_text.lower()
    if any(word in lowered for word in ["solve", "find", "compute", "simplify", "discuss", "prove"]):
        return True
    return False


def looks_like_bare_expression_question(question_text):
    visible_text = strip_math_and_markup(question_text)
    math_tokens = len(re.findall(r"\$|\\\(|\\\[|=", question_text or ""))
    return math_tokens > 0 and len(visible_text) <= 8 and not question_has_task(question_text)


def apply_question_completeness_audit(result, generated_questions):
    blocks = split_numbered_question_blocks(generated_questions)
    for index, block in enumerate(blocks, start=1):
        if looks_like_bare_expression_question(block) or not question_has_task(block):
            add_major_issue(
                result,
                "question",
                index,
                "Question stem has no explicit task; it only gives an expression/formula or lacks an action verb.",
                "Rewrite the question with a clear task such as 求、解、讨论、化简、计算 or 写出, and keep the answer aligned.",
                max_score=78,
            )
            break
    return result


def parse_numeric_recurrence(question_text):
    compact = re.sub(r"\s+", "", question_text or "")
    compact = compact.replace("\\{", "{").replace("\\}", "}")
    match_initial = re.search(r"a_?\{?1\}?=([+-]?\d+)", compact)
    match_recur = re.search(
        r"a_?\{?n\+1\}?=([+-]?\d*)a_?\{?n\}?([+-]\d+)",
        compact,
    )
    if not match_initial or not match_recur:
        return None
    initial = int(match_initial.group(1))
    coeff_text = match_recur.group(1)
    if coeff_text in ["", "+"]:
        coeff = 1
    elif coeff_text == "-":
        coeff = -1
    else:
        coeff = int(coeff_text)
    offset = int(match_recur.group(2))
    return initial, coeff, offset


def parse_geometric_closed_form(answer_text):
    compact = re.sub(r"\s+", "", answer_text or "")
    compact = compact.replace("\\cdot", "*")
    compact = compact.replace("{", "").replace("}", "")
    match = re.search(
        r"a_n=([+-]?\d+)\*?\\?cdot?([+-]?\d+)\^\(?n-1\)?([+-]\d+)?",
        compact,
    )
    if not match:
        match = re.search(r"a_n=([+-]?\d+)\*?([+-]?\d+)\^\(?n-1\)?([+-]\d+)?", compact)
    if not match:
        return None
    scale = int(match.group(1))
    base = int(match.group(2))
    shift = int(match.group(3) or "0")
    return scale, base, shift


def recurrence_value(formula, n):
    scale, base, shift = formula
    return scale * (base ** (n - 1)) + shift


def apply_recurrence_answer_audit(result, generated_questions, answer_key):
    question_blocks = split_numbered_question_blocks(generated_questions)
    answer_blocks = split_numbered_question_blocks(answer_key)
    for index, question in enumerate(question_blocks, start=1):
        recurrence = parse_numeric_recurrence(question)
        if not recurrence:
            continue
        answers_by_index = {idx + 1: block for idx, block in enumerate(answer_blocks)}
        answer = answers_by_index.get(index, "")
        formula = parse_geometric_closed_form(answer)
        if not formula:
            continue
        initial, coeff, offset = recurrence
        a1 = recurrence_value(formula, 1)
        a2 = recurrence_value(formula, 2)
        expected_a2 = coeff * initial + offset
        if a1 != initial or a2 != expected_a2:
            add_major_issue(
                result,
                "answer",
                index,
                "Recurrence closed-form answer fails substitution check for a1 or a2.",
                "Recompute the recurrence formula and verify the first term and one recurrence step before returning.",
                max_score=70,
            )
            break
    return result


def normalize_for_similarity(text):
    text = re.sub(r"\s+", "", text or "")
    text = re.sub(r"[\[\]【】（）(){}#*_`~:：，。,.;；!！?？、\"'“”‘’<>《》]", "", text)
    return text.lower()


def char_ngrams(text, n=2):
    text = normalize_for_similarity(text)
    if len(text) < n:
        return {text} if text else set()
    return {text[index : index + n] for index in range(len(text) - n + 1)}


def similarity_score(left, right):
    left_set = char_ngrams(left)
    right_set = char_ngrams(right)
    if not left_set or not right_set:
        return 0.0
    return len(left_set & right_set) / len(left_set | right_set)


def split_numbered_question_blocks(markdown_text):
    blocks = []
    current = []
    for line in (markdown_text or "").splitlines():
        if re.match(r"^\s*\d+[.)]\s+", line):
            if current:
                blocks.append("\n".join(current).strip())
            current = [line]
        elif current:
            current.append(line)
    if current:
        blocks.append("\n".join(current).strip())
    if not blocks and markdown_text.strip():
        blocks.append(markdown_text.strip())
    return blocks


def build_originality_report(decomposer_result, generated_questions):
    source_items = decomposer_result.get("source_structure", {}).get("items") or []
    source_texts = [item.get("text", "") for item in source_items if item.get("text")]
    generated_blocks = split_numbered_question_blocks(generated_questions)
    pairs = []

    for generated_index, generated_text in enumerate(generated_blocks, start=1):
        best = {"source_index": None, "score": 0.0, "source_text": ""}
        for source_index, source_text in enumerate(source_texts, start=1):
            score = similarity_score(source_text, generated_text)
            if score > best["score"]:
                best = {
                    "source_index": source_index,
                    "score": round(score, 4),
                    "source_text": source_text,
                }
        if best["source_index"] is not None:
            pairs.append(
                {
                    "generated_question_id": str(generated_index),
                    "source_item_id": str(best["source_index"]),
                    "similarity": best["score"],
                    "generated_text": generated_text[:240],
                    "source_text": best["source_text"][:240],
                }
            )

    high_similarity_pairs = [
        pair
        for pair in pairs
        if pair["similarity"] >= 0.72
        and min(len(normalize_for_similarity(pair["generated_text"])), len(normalize_for_similarity(pair["source_text"]))) >= 10
    ]
    return {
        "threshold": 0.72,
        "pairs": pairs,
        "high_similarity_pairs": high_similarity_pairs,
    }


def apply_originality_report(result, originality_report):
    high_pairs = originality_report.get("high_similarity_pairs") or []
    if not high_pairs:
        return result

    pair = max(high_pairs, key=lambda item: item.get("similarity", 0))
    issue = {
        "question_id": pair.get("generated_question_id", "unknown"),
        "severity": "major",
        "problem": (
            "Generated question is too similar to source item "
            f"{pair.get('source_item_id')} (similarity={pair.get('similarity')})."
        ),
        "suggested_fix": (
            "Rewrite this item as a transformed variant: keep the same knowledge point "
            "and difficulty, but change wording and formula surface instead of only changing numbers."
        ),
    }
    result.setdefault("question_issues", [])
    if isinstance(result["question_issues"], list):
        result["question_issues"].insert(0, issue)
    result["is_passed"] = False
    try:
        result["score"] = min(int(result.get("score", 0)), 84)
    except (TypeError, ValueError):
        result["score"] = 84
    instructions = result.setdefault("revision_instructions", [])
    if isinstance(instructions, list):
        instructions.insert(0, issue["suggested_fix"])
    return result


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
    originality_report = build_originality_report(decomposer_result, generated_questions)

    qc_input = {
        "original_exercises": original_exercises,
        "decomposer_result": decomposer_result,
        "generator_result": generator_result,
        "generated_questions": generated_questions,
        "answer_key": answer_key,
        "generation_profile": generation_profile or {},
        "originality_report": originality_report,
    }

    result = call_deepseek_json(
        system_prompt=(
            QC_SYSTEM_PROMPT
            + QC_OUTPUT_GUARDRAILS
            + QC_GENERATION_PROFILE_RULES
            + QC_ANSWER_VERIFICATION_RULES
            + QC_ORIGINALITY_RULES
            + QC_FOCUSED_FEEDBACK_RULES
        ),
        user_content=json.dumps(qc_input, ensure_ascii=False, indent=2),
        max_tokens=6000,
        agent_name="qc",
        model_name=get_model_name("qc"),
)

    apply_originality_report(result, originality_report)
    apply_question_completeness_audit(result, generated_questions)
    apply_recurrence_answer_audit(result, generated_questions, answer_key)
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
