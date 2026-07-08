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
from validators import (
    count_subquestion_markers,
    count_variant_markers,
    has_math_relation,
    is_top_level_line,
    strip_markdown_prefix,
    validate_decomposer_result,
)


DECOMPOSER_SYSTEM_PROMPT = """
你是一个数学练习分析 agent，名字叫 Decomposer。

你的任务是分析用户给出的数学练习，而不是生成新题。

请输出合法 JSON，格式如下：

{
  "topic": "主要知识点",
  "subtopics": ["次级知识点1", "次级知识点2"],
  "grade_level": "大致年级",
  "question_types": ["题型1", "题型2"],
  "format_structure": "题目格式结构",
  "difficulty_distribution": {
    "easy": 0,
    "medium": 0,
    "hard": 0
  },
  "style_features": ["风格特点1", "风格特点2"],
  "latex_usage": "LaTeX 使用情况",
  "generation_constraints": ["后续仿写新题时要遵守的约束"],
  "uncertain_points": ["不确定或需要人工确认的地方"]
}

要求：
1. 只输出 JSON。
2. 不要输出 Markdown。
3. 不要解释。
"""


DECOMPOSER_REVISION_SYSTEM_PROMPT = """
你是一个数学练习分析 agent，名字叫 Decomposer。

你之前已经分析过一组数学练习，但 Reviewer 认为你的分析存在问题。
现在你需要根据 Reviewer 的反馈，重新输出一版更准确、更完整的分析 JSON。

你会收到三部分内容：
1. original_exercises：原始数学练习
2. previous_decomposer_result：你上一轮输出的分析
3. reviewer_feedback：Reviewer 的审核意见

你必须：
1. 保留上一轮中正确的部分。
2. 修正 Reviewer 指出的错误。
3. 补充 Reviewer 指出的遗漏。
4. 如果 Reviewer 的意见不合理，可以不采纳，但要在 uncertain_points 中说明。
5. 输出结构必须和上一轮 Decomposer 完全一致。

请输出合法 JSON，格式如下：

{
  "topic": "主要知识点",
  "subtopics": ["次级知识点1", "次级知识点2"],
  "grade_level": "大致年级",
  "question_types": ["题型1", "题型2"],
  "format_structure": "题目格式结构",
  "difficulty_distribution": {
    "easy": 0,
    "medium": 0,
    "hard": 0
  },
  "style_features": ["风格特点1", "风格特点2"],
  "latex_usage": "LaTeX 使用情况",
  "generation_constraints": ["后续仿写新题时要遵守的约束"],
  "uncertain_points": ["不确定或需要人工确认的地方"]
}

要求：
1. 只输出 JSON。
2. 不要输出 Markdown。
3. 不要解释。
"""


DECOMPOSER_STRUCTURE_RULES = """

Strict structure rules:
1. The input may be a worksheet with many subquestions. Do not treat it as a single question just because it has one topic.
2. Use source_structure.total_question_count as the authoritative detected item count.
3. Count numbered subquestions such as (1), (2), ... as separate question items.
4. Count variants such as "变式 1" as separate question items.
5. Include source_structure in your JSON output exactly as provided in user_content.
6. difficulty_distribution should describe all detected question items, not only one representative item.
"""


def analyze_input_structure(markdown_text):
    lines = markdown_text.splitlines()
    sections = []
    current_section = None
    pending_top_level = None
    top_level_count = 0
    subquestion_count = 0
    variant_count = 0
    items = []

    def ensure_section(name):
        nonlocal current_section
        if current_section is None or current_section["section"] != name:
            current_section = {
                "section": name,
                "top_level_count": 0,
                "subquestion_count": 0,
                "variant_count": 0,
            }
            sections.append(current_section)
        return current_section

    def flush_pending_top_level(has_subitems=False):
        nonlocal pending_top_level, top_level_count
        if pending_top_level is None:
            return
        if not has_subitems:
            top_level_count += 1
            pending_top_level["section_data"]["top_level_count"] += 1
            items.append(
                {
                    "id": len(items) + 1,
                    "kind": "top_level",
                    "section": pending_top_level["section_data"]["section"],
                    "text": pending_top_level["line"],
                }
            )
        pending_top_level = None

    ensure_section("default")

    for raw_line in lines:
        stripped = raw_line.strip()
        if not stripped:
            continue

        if stripped.startswith("##"):
            flush_pending_top_level()
            section_name = stripped.lstrip("#").strip()
            ensure_section(section_name)
            continue

        section_data = ensure_section(current_section["section"])
        normalized = strip_markdown_prefix(stripped)
        is_top_level = is_top_level_line(normalized)
        is_subquestion = count_subquestion_markers(normalized) > 0
        is_variant = count_variant_markers(normalized) > 0

        if is_top_level:
            flush_pending_top_level()
            pending_top_level = {"line": stripped, "section_data": section_data}
            continue

        if is_subquestion:
            if pending_top_level is not None:
                flush_pending_top_level(has_subitems=True)
            subquestion_count += 1
            section_data["subquestion_count"] += 1
            items.append(
                {
                    "id": len(items) + 1,
                    "kind": "subquestion",
                    "section": section_data["section"],
                    "text": stripped,
                }
            )
            continue

        if is_variant:
            flush_pending_top_level()
            variant_count += 1
            section_data["variant_count"] += 1
            items.append(
                {
                    "id": len(items) + 1,
                    "kind": "variant",
                    "section": section_data["section"],
                    "text": stripped,
                }
            )
            continue

        if pending_top_level is not None and has_math_relation(normalized):
            flush_pending_top_level()

    flush_pending_top_level()

    total_question_count = top_level_count + subquestion_count + variant_count
    return {
        "top_level_question_count": top_level_count,
        "subquestion_count": subquestion_count,
        "variant_count": variant_count,
        "total_question_count": total_question_count,
        "items": items,
        "sections": sections,
        "counting_rule": "top-level items without subquestions + numbered subquestions + variants",
    }


def attach_source_structure(result, source_structure):
    result["source_structure"] = source_structure
    result["detected_question_count"] = source_structure["total_question_count"]
    distribution = result.get("difficulty_distribution", {})
    if isinstance(distribution, dict):
        distribution_total = sum(
            value
            for value in distribution.values()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        )
        if distribution_total not in [0, source_structure["total_question_count"]]:
            uncertain_points = result.setdefault("uncertain_points", [])
            if isinstance(uncertain_points, list):
                uncertain_points.append(
                    "difficulty_distribution count differs from deterministic source_structure.total_question_count; source_structure is authoritative."
                )
    return result


def run(input_path, output_path):
    markdown_text = read_text_file(input_path)
    source_structure = analyze_input_structure(markdown_text)

    if not markdown_text.strip():
        raise ValueError("输入题目为空")

    decomposer_input = {
        "source_structure": source_structure,
        "original_exercises": markdown_text,
    }

    result = call_deepseek_json(
        system_prompt=DECOMPOSER_SYSTEM_PROMPT + DECOMPOSER_STRUCTURE_RULES,
        user_content=json.dumps(decomposer_input, ensure_ascii=False, indent=2),
        max_tokens=2000,
        agent_name="decomposer",
        model_name=get_model_name("decomposer"),
    )

    attach_source_structure(result, source_structure)
    validate_decomposer_result(result)
    write_json_file(output_path, result)

    print(f"Decomposer 完成，结果已写入：{output_path}")


def revise(input_path, previous_decomposer_path, reviewer_feedback_path, output_path):
    original_exercises = read_text_file(input_path)
    source_structure = analyze_input_structure(original_exercises)
    previous_decomposer_result = read_json_file(previous_decomposer_path)
    reviewer_feedback = read_json_file(reviewer_feedback_path)

    revision_input = {
        "source_structure": source_structure,
        "original_exercises": original_exercises,
        "previous_decomposer_result": previous_decomposer_result,
        "reviewer_feedback": reviewer_feedback,
    }

    result = call_deepseek_json(
        system_prompt=DECOMPOSER_REVISION_SYSTEM_PROMPT + DECOMPOSER_STRUCTURE_RULES,
        user_content=json.dumps(revision_input, ensure_ascii=False, indent=2),
        max_tokens=2000,
        agent_name="decomposer_revise",
        model_name=get_model_name("decomposer"),
)

    attach_source_structure(result, source_structure)
    validate_decomposer_result(result)
    write_json_file(output_path, result)

    print(f"Decomposer 已根据 Reviewer 意见重做，结果已写入：{output_path}")


def main():
    if len(sys.argv) == 3:
        input_path = sys.argv[1]
        output_path = sys.argv[2]
        run(input_path, output_path)
        return

    if len(sys.argv) == 6 and sys.argv[1] == "--revise":
        input_path = sys.argv[2]
        previous_decomposer_path = sys.argv[3]
        reviewer_feedback_path = sys.argv[4]
        output_path = sys.argv[5]

        revise(
            input_path,
            previous_decomposer_path,
            reviewer_feedback_path,
            output_path,
        )
        return

    print("普通用法：")
    print("python decomposer_agent.py <输入md文件> <输出json文件>")
    print()
    print("打回重做用法：")
    print("python decomposer_agent.py --revise <输入md文件> <上一轮decomposer输出> <reviewer输出> <新输出json文件>")


if __name__ == "__main__":
    main()
