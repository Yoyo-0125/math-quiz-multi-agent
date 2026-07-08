import json
import re
import sys

from common import (
    call_deepseek_json,
    get_model_name,
    read_json_file,
    read_text_file,
    write_json_file,
    write_text_file,
)
from math_checks import validate_basic_math_consistency
from validators import validate_generator_result, validate_question_answer_pair


GENERATOR_SYSTEM_PROMPT = """
你是一个数学题目生成 agent，名字叫 Generator。

你的任务是根据原始数学练习和 Decomposer 的分析，生成一组“同风格、同难度、同知识点”的新题，并单独给出答案。

你会收到三部分内容：
1. original_exercises：原始数学练习
2. decomposer_result：Decomposer 对原始练习的分析
3. reviewer_result：Reviewer 对分析结果的审核意见

你必须遵守：
1. 不要直接复制原题。
2. 新题要和原题知识点接近。
3. 新题要和原题格式接近。
4. 新题数量要和原题数量接近。
5. 使用中文题干。
6. 数学表达式尽量使用 LaTeX。
7. 题目和答案必须分开。
8. 答案要尽量简洁，但不能错。
9. 如果 reviewer_result 中指出了分析问题，你要优先参考修正后的方向。
10. 不要生成超出原题难度太多的内容。

请输出合法 JSON，格式如下：

{
  "questions_markdown": "这里放新题 Markdown，不要包含答案",
  "answer_key_markdown": "这里放答案 Markdown",
  "generation_notes": [
    "说明你如何保持了原题风格",
    "说明你如何控制了难度"
  ],
  "possible_risks": [
    "可能存在的问题或需要 QC 检查的地方"
  ]
}

要求：
1. 只输出 JSON。
2. 不要输出 Markdown 代码块。
3. 不要在 JSON 外面加解释。
4. questions_markdown 和 answer_key_markdown 必须是字符串。
5. 可以在内部推理和验算，但不要把推理草稿、犹豫过程、错误尝试、自我纠正过程写进 questions_markdown 或 answer_key_markdown。
6. answer_key_markdown 只写最终答案和必要的简洁步骤；如果需要分类讨论，必须给出完整、确定、可检查的分类结论。
7. 如果题目会导致答案过度复杂或分类讨论失控，请生成更接近原题难度的新题，而不是把复杂草稿塞进答案。
"""


GENERATOR_REVISION_SYSTEM_PROMPT = """
你是一个数学题目生成 agent，名字叫 Generator。

你之前生成了一组数学题和答案，但 QC 发现存在问题。
现在你需要根据 QC 的反馈修正题目和答案。

你会收到七部分内容：
1. original_exercises：原始数学练习
2. decomposer_result：Decomposer 对原始练习的分析
3. reviewer_result：Reviewer 对分析结果的审核意见
4. previous_generator_result：你上一轮的完整 JSON 输出
5. previous_questions：你上一轮生成的新题
6. previous_answer_key：你上一轮生成的答案
7. qc_feedback：QC 的质量检查结果

你必须：
1. 优先修复 QC 指出的 major 问题。
2. 尽量修复 QC 指出的 minor 问题。
3. 保持题目和答案分离。
4. 修正后题目数量仍应和原题接近。
5. 不要为了修错而明显改变知识点、格式和难度。
6. 答案必须和题目一一对应。
7. 如果某条 QC 意见明显不合理，可以不采纳，但必须在 possible_risks 中说明。

请输出合法 JSON，格式如下：

{
  "questions_markdown": "这里放修正后的新题 Markdown，不要包含答案",
  "answer_key_markdown": "这里放修正后的答案 Markdown",
  "generation_notes": [
    "说明你修正了哪些问题",
    "说明你如何保持了原题风格"
  ],
  "possible_risks": [
    "仍可能存在的问题或需要继续 QC 检查的地方"
  ]
}

要求：
1. 只输出 JSON。
2. 不要输出 Markdown 代码块。
3. 不要在 JSON 外面加解释。
4. questions_markdown 和 answer_key_markdown 必须是字符串。
5. 可以在内部推理和验算，但不要把推理草稿、犹豫过程、错误尝试、自我纠正过程写进 questions_markdown 或 answer_key_markdown。
6. answer_key_markdown 只写最终修正后的答案和必要的简洁步骤；如果需要分类讨论，必须给出完整、确定、可检查的分类结论。
"""


GENERATOR_OUTPUT_GUARDRAILS = """

额外硬性规则：
1. 你可以在内部使用 thinking mode 推理，但最终 JSON 里只能出现成稿，不能出现推理草稿。
2. answer_key_markdown 禁止出现这些内容：重新计算、不确定、可能、需要进一步、略、待补、？、不对、矛盾、建议答案、默认答案。
3. 生成新题前先做可解性检查：新题必须能在不超过 3 个大分类的情况下写出完整答案。
4. 如果原题是含参题，新题可以换参数名，但不要额外增加新的临界点；优先保持原题的因式结构和分类复杂度。
5. 对含参不等式，必须选择能完整分类的系数。若分类会超过 3 个大类，请换一组更简单的数，而不是输出复杂答案。
6. answer_key_markdown 必须使用统一编号格式，例如：
   1. 最终答案：...
      简要步骤：...
7. questions_markdown 只放题目，不能泄露答案；answer_key_markdown 只放答案，不能包含自我评价或草稿。
8. generation_notes 可以简短说明如何控制难度；possible_risks 只列真正需要 QC 注意的点，不要把未完成答案放进去。
9. 如果你无法给出完整、确定、可检查的答案，必须重新生成一道更简单但同知识点的新题。
10. 必须遵守 user_content 中的 generation_options.max_generated_questions。若原始输入是一整套题集，只抽取最有代表性的题型生成同类题，不要生成整套题集。
11. questions_markdown 中生成的主问题数量不得超过 generation_options.max_generated_questions；除非 max_generated_questions 大于 1，否则不要输出多个大题、多个章节或题组。
12. answer_key_markdown 必须与 questions_markdown 的题目数量一一对应，且答案总长度应控制在 1200 汉字以内。
"""


GENERATOR_STRICT_SELF_CHECK = """

Strict self-check before final JSON:
1. Do not put known answer mistakes, missing cases, or "needs correction" notes into possible_risks.
2. If you discover an answer is wrong or incomplete, regenerate a simpler question and a complete verified answer before returning JSON.
3. possible_risks may only contain external QC concerns, not unresolved defects in your own answer.
4. For quadratic inequality questions, choose coefficients that factor cleanly over simple linear factors.
5. Before returning JSON, expand every factorization used in the answer and verify it equals the question expression.
6. If the factorization and the question expression do not match exactly, regenerate the question-answer pair.
7. For every inequality answer, test one value from each interval against the generated question before finalizing.
8. For parameterized inequalities, list the critical parameter order first internally, then verify every case covers all parameter values without overlap.
9. If more than two answers feel uncertain during self-check, regenerate the whole worksheet with simpler coefficients instead of returning weak answers.
10. Avoid generating quadratic inequalities where the leading coefficient itself changes sign with the parameter, unless the source item explicitly requires that pattern.
11. If a leading coefficient can be positive, zero, or negative, solve the zero case separately and verify the negative-leading-coefficient case by substituting sample values; otherwise regenerate a simpler same-knowledge-point item.
12. For forms like (t-c)x^2+px+q >= 0, state the double-root case separately and avoid hiding it inside a union that looks like two intervals.
13. For rational inequalities, always state the denominator exclusion point and verify the final cases after comparing both critical points.
"""


def compact_original_exercises(markdown_text, max_problem_lines=12):
    lines = markdown_text.splitlines()
    if len(lines) <= max_problem_lines + 8:
        return markdown_text, False

    kept = []
    problem_lines = 0
    seen_normalized = set()

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        is_header = stripped.startswith("#")
        looks_like_problem = (
            "$" in stripped
            or stripped.startswith(("-", "*"))
            or stripped[:2].isdigit()
            or stripped.startswith(("(", "（"))
            or "求" in stripped
            or "证明" in stripped
            or "解" in stripped
        )

        if is_header:
            kept.append(line)
            continue

        if not looks_like_problem:
            continue

        normalized = "".join(ch for ch in stripped if not ch.isdigit())[:80]
        if normalized in seen_normalized:
            continue

        kept.append(line)
        seen_normalized.add(normalized)
        problem_lines += 1

        if problem_lines >= max_problem_lines:
            break

    if not kept:
        return markdown_text, False

    kept.append("")
    kept.append(
        "[Generator input compacted: original input has many similar items; "
        "use these representative items plus decomposer_result to generate concise similar questions.]"
    )
    return "\n".join(kept), True


def build_generation_profile(max_generated_questions=1, generation_profile=None):
    profile = dict(generation_profile or {})
    profile.setdefault("question_count_mode", "fixed")
    question_count = profile.get("question_count", max_generated_questions)
    try:
        question_count = int(question_count)
    except (TypeError, ValueError):
        question_count = int(max_generated_questions)

    question_count = max(1, question_count)
    profile["question_count"] = question_count
    profile["max_generated_questions"] = question_count
    profile.setdefault("difficulty", "match_input")
    profile.setdefault("knowledge_points", "")
    profile.setdefault("question_types", "")
    profile.setdefault("style", "match_input")
    profile.setdefault("answer_detail", "concise")
    profile.setdefault("allow_input_compaction", True)
    if profile.get("question_count_mode") == "match_source":
        profile["allow_input_compaction"] = False
    return profile


def apply_source_question_count(profile, decomposer_result):
    if profile.get("question_count_mode") != "match_source":
        return profile

    source_structure = decomposer_result.get("source_structure", {})
    source_count = source_structure.get("total_question_count")
    if isinstance(source_count, int) and source_count > 0:
        profile = dict(profile)
        profile["question_count"] = source_count
        profile["max_generated_questions"] = source_count
    return profile


def build_generation_instruction(profile, revision=False):
    action = "revise" if revision else "generate"
    instruction = (
        f"You must {action} exactly {profile['question_count']} numbered question(s), "
        f"not fewer and not more. Number questions as (1), (2), ... in questions_markdown, "
        f"and number answers as 1., 2., ... in answer_key_markdown. "
        f"Difficulty target: {profile.get('difficulty')}. "
        f"Knowledge point focus: {profile.get('knowledge_points') or 'match decomposer_result'}. "
        f"Question type focus: {profile.get('question_types') or 'match input'}. "
        f"Style target: {profile.get('style')}. "
        f"Answer detail: {profile.get('answer_detail')}. "
        "The answer count must equal the question count exactly. "
        "Use decomposer_result.source_structure to understand the original worksheet item count and section structure."
    )
    if revision:
        instruction += (
            " QC feedback is focused on one target question; use it as the most "
            "important example of the failure mode. Still return the full requested "
            "output count, not only the focused question."
        )
    if profile.get("question_count_mode") == "match_source":
        instruction += (
            " The default mode is one-to-one source matching: generate one similar "
            "question for each item in decomposer_result.source_structure.items, "
            "preserving item order and broad section/type coverage."
        )
    return instruction


def issue_priority(issue):
    if not isinstance(issue, dict):
        return 9
    return 0 if issue.get("severity") == "major" else 1


def get_primary_qc_issue(qc_feedback):
    issues = []
    for key in ["answer_issues", "question_issues", "style_issues"]:
        for issue in qc_feedback.get(key, []) or []:
            if isinstance(issue, dict):
                copied = dict(issue)
                copied["issue_type"] = key
                issues.append(copied)

    if not issues:
        return None

    return sorted(issues, key=issue_priority)[0]


def extract_question_number(question_id):
    if not question_id:
        return None
    match = re.search(r"\d+", str(question_id))
    if not match:
        return None
    return int(match.group(0))


def split_numbered_blocks(markdown_text):
    blocks = {}
    current_number = None
    current_lines = []

    for line in markdown_text.splitlines():
        stripped = line.strip().lstrip("*#> -")
        match = re.match(r"^\(?(\d+)\)?[\.、．)]", stripped)
        if not match:
            match = re.match(r"^（(\d+)）", stripped)

        if match:
            if current_number is not None:
                blocks[current_number] = "\n".join(current_lines).strip()
            current_number = int(match.group(1))
            current_lines = [line]
        elif current_number is not None:
            current_lines.append(line)

    if current_number is not None:
        blocks[current_number] = "\n".join(current_lines).strip()

    return blocks


def build_focused_revision_context(previous_questions, previous_answer_key, qc_feedback):
    primary_issue = get_primary_qc_issue(qc_feedback)
    if not primary_issue:
        return previous_questions, previous_answer_key, qc_feedback

    question_number = extract_question_number(primary_issue.get("question_id"))
    if question_number is None:
        return previous_questions, previous_answer_key, {
            "focused_issue": primary_issue,
            "revision_instructions": qc_feedback.get("revision_instructions", [])[:1],
        }

    question_blocks = split_numbered_blocks(previous_questions)
    answer_blocks = split_numbered_blocks(previous_answer_key)

    focused_feedback = {
        "is_passed": qc_feedback.get("is_passed", False),
        "score": qc_feedback.get("score", 0),
        "target_question_number": question_number,
        "focused_issue": primary_issue,
        "revision_instructions": qc_feedback.get("revision_instructions", [])[:1],
        "qc_summary": qc_feedback.get("qc_summary", ""),
    }

    return (
        question_blocks.get(question_number, previous_questions),
        answer_blocks.get(question_number, previous_answer_key),
        focused_feedback,
    )


def save_generator_outputs(
    result,
    output_json_path,
    questions_output_path,
    answer_key_output_path,
    max_generated_questions=1,
    expected_question_count=None,
):
    validate_generator_result(result)

    questions_markdown = result.get("questions_markdown", "")
    answer_key_markdown = result.get("answer_key_markdown", "")

    if not questions_markdown.strip():
        raise RuntimeError("Generator 没有生成 questions_markdown")

    if not answer_key_markdown.strip():
        raise RuntimeError("Generator 没有生成 answer_key_markdown")

    validate_question_answer_pair(
        questions_markdown,
        answer_key_markdown,
        expected_question_count=expected_question_count,
    )
    validate_basic_math_consistency(questions_markdown, answer_key_markdown)

    write_json_file(output_json_path, result)
    write_text_file(questions_output_path, questions_markdown)
    write_text_file(answer_key_output_path, answer_key_markdown)

    print(f"Generator JSON 已写入：{output_json_path}")
    print(f"新题已写入：{questions_output_path}")
    print(f"答案已写入：{answer_key_output_path}")


def run(
    original_input_path,
    decomposer_json_path,
    reviewer_json_path,
    output_json_path,
    questions_output_path,
    answer_key_output_path,
    max_generated_questions=1,
    generation_profile=None,
):
    original_exercises = read_text_file(original_input_path)
    profile = build_generation_profile(max_generated_questions, generation_profile)
    if profile.get("allow_input_compaction", True):
        representative_exercises, input_was_compacted = compact_original_exercises(
            original_exercises
        )
    else:
        representative_exercises, input_was_compacted = original_exercises, False
    decomposer_result = read_json_file(decomposer_json_path)
    profile = apply_source_question_count(profile, decomposer_result)
    reviewer_result = read_json_file(reviewer_json_path)

    generator_input = {
        "original_exercises": representative_exercises,
        "decomposer_result": decomposer_result,
        "reviewer_result": reviewer_result,
        "generation_options": {
            **profile,
            "mode": "representative_sample",
            "input_was_compacted": input_was_compacted,
            "instruction": build_generation_instruction(profile),
        },
    }

    output_token_budget = max(12000, profile["question_count"] * 2500)

    result = call_deepseek_json(
        system_prompt=(
            GENERATOR_SYSTEM_PROMPT
            + GENERATOR_OUTPUT_GUARDRAILS
            + GENERATOR_STRICT_SELF_CHECK
        ),
        user_content=json.dumps(generator_input, ensure_ascii=False, indent=2),
        max_tokens=output_token_budget,
        agent_name="generator",
        model_name=get_model_name("generator"),
    )

    save_generator_outputs(
        result,
        output_json_path,
        questions_output_path,
        answer_key_output_path,
        max_generated_questions=profile["question_count"],
        expected_question_count=profile["question_count"],
    )

    print("Generator 首次生成完成。")


def revise(
    original_input_path,
    decomposer_json_path,
    reviewer_json_path,
    previous_generator_json_path,
    previous_questions_path,
    previous_answer_key_path,
    qc_feedback_path,
    output_json_path,
    questions_output_path,
    answer_key_output_path,
    max_generated_questions=1,
    generation_profile=None,
):
    original_exercises = read_text_file(original_input_path)
    profile = build_generation_profile(max_generated_questions, generation_profile)
    if profile.get("allow_input_compaction", True):
        representative_exercises, input_was_compacted = compact_original_exercises(
            original_exercises
        )
    else:
        representative_exercises, input_was_compacted = original_exercises, False
    decomposer_result = read_json_file(decomposer_json_path)
    profile = apply_source_question_count(profile, decomposer_result)
    reviewer_result = read_json_file(reviewer_json_path)

    previous_generator_result = read_json_file(previous_generator_json_path)
    previous_questions = read_text_file(previous_questions_path)
    previous_answer_key = read_text_file(previous_answer_key_path)
    qc_feedback = read_json_file(qc_feedback_path)
    focused_questions, focused_answer_key, focused_qc_feedback = (
        build_focused_revision_context(previous_questions, previous_answer_key, qc_feedback)
    )

    revision_input = {
        "original_exercises": representative_exercises,
        "decomposer_result": decomposer_result,
        "reviewer_result": reviewer_result,
        "previous_generator_result_summary": {
            "generation_notes": previous_generator_result.get("generation_notes", []),
            "possible_risks": previous_generator_result.get("possible_risks", []),
        },
        "focused_previous_question": focused_questions,
        "focused_previous_answer": focused_answer_key,
        "focused_qc_feedback": focused_qc_feedback,
        "generation_options": {
            **profile,
            "mode": "representative_sample",
            "input_was_compacted": input_was_compacted,
            "revision_mode": "single_issue_focused_retry",
            "instruction": build_generation_instruction(profile, revision=True),
        },
    }

    output_token_budget = max(12000, profile["question_count"] * 2500)

    result = call_deepseek_json(
        system_prompt=(
            GENERATOR_REVISION_SYSTEM_PROMPT
            + GENERATOR_OUTPUT_GUARDRAILS
            + GENERATOR_STRICT_SELF_CHECK
        ),
        user_content=json.dumps(revision_input, ensure_ascii=False, indent=2),
        max_tokens=output_token_budget,
        agent_name="generator_revise",
        model_name=get_model_name("generator"),
)
    save_generator_outputs(
        result,
        output_json_path,
        questions_output_path,
        answer_key_output_path,
        max_generated_questions=profile["question_count"],
        expected_question_count=profile["question_count"],
    )

    print("Generator 已根据 QC 意见重做。")


def main():
    if len(sys.argv) == 7:
        original_input_path = sys.argv[1]
        decomposer_json_path = sys.argv[2]
        reviewer_json_path = sys.argv[3]
        output_json_path = sys.argv[4]
        questions_output_path = sys.argv[5]
        answer_key_output_path = sys.argv[6]

        run(
            original_input_path,
            decomposer_json_path,
            reviewer_json_path,
            output_json_path,
            questions_output_path,
            answer_key_output_path,
        )
        return

    if len(sys.argv) == 12 and sys.argv[1] == "--revise":
        original_input_path = sys.argv[2]
        decomposer_json_path = sys.argv[3]
        reviewer_json_path = sys.argv[4]
        previous_generator_json_path = sys.argv[5]
        previous_questions_path = sys.argv[6]
        previous_answer_key_path = sys.argv[7]
        qc_feedback_path = sys.argv[8]
        output_json_path = sys.argv[9]
        questions_output_path = sys.argv[10]
        answer_key_output_path = sys.argv[11]

        revise(
            original_input_path,
            decomposer_json_path,
            reviewer_json_path,
            previous_generator_json_path,
            previous_questions_path,
            previous_answer_key_path,
            qc_feedback_path,
            output_json_path,
            questions_output_path,
            answer_key_output_path,
        )
        return

    print("普通生成用法：")
    print(
        "python generator_agent.py "
        "<原始md文件> "
        "<decomposer输出json> "
        "<reviewer输出json> "
        "<generator输出json> "
        "<新题输出md> "
        "<答案输出md>"
    )
    print()
    print("根据 QC 打回重做用法：")
    print(
        "python generator_agent.py --revise "
        "<原始md文件> "
        "<decomposer输出json> "
        "<reviewer输出json> "
        "<上一轮generator输出json> "
        "<上一轮新题md> "
        "<上一轮答案md> "
        "<qc反馈json> "
        "<新generator输出json> "
        "<新题输出md> "
        "<答案输出md>"
    )


if __name__ == "__main__":
    main()
