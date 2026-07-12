import json
import re
import sys
from pathlib import Path

from common import (
    call_deepseek_json,
    get_model_name,
    read_text_file,
    write_json_file,
    write_text_file,
)
from validators import (
    count_question_items,
    count_subquestion_markers,
    count_variant_markers,
    has_math_relation,
    is_top_level_line,
    strip_markdown_prefix,
)


READER_SYSTEM_PROMPT = """
你是数学练习输入整理 agent，名字叫 Reader。

你的任务是把用户粘贴、OCR 或 UI 转存后可能混乱的数学题输入整理成后续 agent 可读的题目清单。

请输出合法 JSON，格式如下：
{
  "clean_markdown": "整理后的 Markdown 题目清单，只包含题目，不包含答案，不包含解释",
  "expected_question_count": 0,
  "warnings": ["整理中不确定的地方"]
}

要求：
1. 只输出 JSON，不要输出 Markdown 外壳。
2. clean_markdown 只保留题目本身，不能附加“原始输入”“说明”“答案”。
3. 尽量恢复原有章节、编号、小题和变式结构。
4. 不要把单独的公式碎片误判为独立题目；应把它合并回相邻题干。
5. 如果原文存在乱码或缺字，保留可判断的数学结构，并在 warnings 中说明。
6. expected_question_count 按可作答的题目/小题/变式数量统计。
"""


def split_inline_items(text):
    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    patterns = [
        r"(?<!^)(?=\n?#{1,3}\s+)",
        r"(?<!^)(?=\n?\d+\.\s)",
        r"(?<!^)(?=\n?[*+-]\s*\*\*[\(（]\d+[\)）]\*\*)",
        r"(?<!^)(?=\n?\*\*[\(（]\d+[\)）]\*\*)",
        r"(?<!^)(?=\n?[*+-]\s*\*\*(?:变式|Variant)\s*\d+)",
        r"(?<!^)(?=\n?\*\*(?:变式|Variant)\s*\d+)",
    ]
    for pattern in patterns:
        normalized = re.sub(pattern, "\n", normalized, flags=re.IGNORECASE)
    return "\n".join(line.rstrip() for line in normalized.splitlines())


def clean_item_text(text):
    cleaned = strip_markdown_prefix(text)
    cleaned = re.sub(r"^\*\s*", "", cleaned)
    cleaned = re.sub(r"^\*\*([\(\uFF08]\d+[\)\uFF09])\*\*\s*", r"\1 ", cleaned)
    cleaned = re.sub(r"^\*\*((?:变式|Variant)\s*\d+[^*]*)\*\*\s*", r"\1 ", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.strip("*_ -")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def is_choice_option_line(text):
    return re.match(r"^[A-H]\s*[.．、)]\s*", str(text or "").strip(), re.IGNORECASE) is not None


def is_solution_fragment(text):
    text = str(text or "").strip()
    if not text:
        return True
    if is_top_level_line(text):
        return False
    solution_markers = [
        "代入",
        "综上",
        "因此",
        "所以",
        "故",
        "得",
        "答案",
        "最终答案",
        "分",
        "\\therefore",
    ]
    task_markers = [
        "求",
        "解",
        "证明",
        "判断",
        "计算",
        "讨论",
        "已知",
        "若",
        "如图",
        "设",
        "比较",
        "选择",
        "写出",
        "化为",
    ]
    has_solution_marker = any(marker in text for marker in solution_markers)
    has_task_marker = any(marker in text for marker in task_markers)
    if has_solution_marker and not has_task_marker:
        return True
    if len(text) <= 18 and has_math_relation(text) and not has_task_marker:
        return True
    return False


def reader_duplicate_key(text):
    text = re.sub(r"^\s*\d+\s*[.．、:)]\s*", "", str(text or ""))
    text = re.sub(r"\s+", "", text)
    return text[:220]


def postprocess_reader_items(items):
    processed = []
    seen = {}
    for item in items:
        text = clean_item_text(item.get("text", ""))
        if not text:
            continue
        if is_choice_option_line(text):
            if processed:
                processed[-1]["text"] = (processed[-1]["text"].rstrip() + "\n" + text).strip()
            continue
        if item.get("kind") == "implicit" and is_solution_fragment(text):
            continue
        key = reader_duplicate_key(text)
        if len(key) >= 24:
            seen[key] = seen.get(key, 0) + 1
            if seen[key] > 1:
                continue
        next_item = dict(item)
        next_item["id"] = len(processed) + 1
        next_item["text"] = text
        processed.append(next_item)
    return processed


def is_heading(line):
    return line.strip().startswith("#")


def heading_text(line):
    return line.strip().lstrip("#").strip()


def extract_reader_items(markdown_text):
    normalized_text = split_inline_items(markdown_text)
    lines = [line.strip() for line in normalized_text.splitlines()]
    items = []
    sections = []
    current_section = "default"
    pending_header = None
    warnings = []

    def add_section(name):
        nonlocal current_section
        current_section = name or "default"
        if current_section not in sections:
            sections.append(current_section)

    def add_item(kind, text, source_line):
        cleaned = clean_item_text(text)
        if not cleaned:
            return
        items.append(
            {
                "id": len(items) + 1,
                "kind": kind,
                "section": current_section,
                "text": cleaned,
                "source_line": source_line,
            }
        )

    add_section(current_section)

    for raw_line in lines:
        if not raw_line:
            continue
        if is_heading(raw_line):
            pending_header = None
            add_section(heading_text(raw_line))
            continue

        normalized = strip_markdown_prefix(raw_line)
        if not normalized:
            continue

        if is_choice_option_line(normalized) and items:
            items[-1]["text"] = (items[-1]["text"].rstrip() + "\n" + clean_item_text(raw_line)).strip()
            pending_header = None
            continue

        is_variant = count_variant_markers(normalized) > 0
        is_subquestion = count_subquestion_markers(normalized) > 0
        is_top_level = is_top_level_line(normalized)

        if is_variant:
            pending_header = None
            add_item("variant", raw_line, raw_line)
            continue

        if is_subquestion:
            pending_header = None
            add_item("subquestion", raw_line, raw_line)
            continue

        if is_top_level:
            if has_math_relation(normalized):
                pending_header = None
                add_item("top_level", raw_line, raw_line)
            else:
                pending_header = clean_item_text(raw_line)
            continue

        if pending_header and has_math_relation(normalized):
            add_item("top_level", f"{pending_header} {raw_line}", raw_line)
            pending_header = None
            continue

        if has_math_relation(normalized):
            add_item("implicit", raw_line, raw_line)
            continue

    if pending_header:
        warnings.append(f"Unresolved heading-like item without math content: {pending_header}")

    items = postprocess_reader_items(items)
    return normalized_text, items, sections, warnings


def needs_model_reader(original_count, items):
    if not items:
        return True
    implicit_count = sum(1 for item in items if item.get("kind") == "implicit")
    short_fragment_count = sum(
        1 for item in items if len(item.get("text", "").strip()) <= 12
    )
    if original_count <= 2 and len(items) >= 6:
        return True
    if implicit_count >= max(4, len(items) // 2):
        return True
    if short_fragment_count >= 4:
        return True
    return False


def run_model_reader(original_text):
    result = call_deepseek_json(
        system_prompt=READER_SYSTEM_PROMPT,
        user_content=json.dumps({"raw_input": original_text}, ensure_ascii=False, indent=2),
        max_tokens=12000,
        agent_name="reader",
        model_name=get_model_name("reader"),
        response_format_json=True,
    )
    clean_markdown = str(result.get("clean_markdown", "")).strip()
    if not clean_markdown:
        raise ValueError("Reader model did not return clean_markdown")

    warnings = result.get("warnings", [])
    if isinstance(warnings, str):
        warnings = [warnings]
    if not isinstance(warnings, list):
        warnings = ["Reader model returned non-list warnings"]

    expected_count = result.get("expected_question_count")
    return clean_markdown, expected_count, warnings


def render_clean_input(original_text, items, sections, warnings):
    lines = [
        "# Reader Clean Input",
        "",
        "> This file is generated by Reader. It normalizes the user input into a readable question list for later agents.",
        "",
    ]

    if warnings:
        lines.append("## Reader Warnings")
        for warning in warnings:
            lines.append(f"- {warning}")
        lines.append("")

    lines.append("## Question List")
    for item in items:
        section = item.get("section") or "default"
        lines.append(f"{item['id']}. [{section}] {item['text']}")

    lines.append("")
    return "\n".join(lines)


def run(input_path, output_json_path, clean_input_path):
    original_text = read_text_file(input_path)
    if not original_text.strip():
        raise ValueError("Reader received empty input")

    normalized_text, items, sections, warnings = extract_reader_items(original_text)
    original_count = count_question_items(normalized_text)
    used_model_reader = False
    model_expected_question_count = None

    if needs_model_reader(original_count, items):
        model_clean_markdown, model_expected_question_count, model_warnings = run_model_reader(
            original_text
        )
        normalized_text, items, sections, warnings = extract_reader_items(model_clean_markdown)
        warnings = list(warnings) + [
            f"Model Reader fallback was used; expected_question_count={model_expected_question_count}."
        ] + [str(warning) for warning in model_warnings]
        used_model_reader = True

    cleaned_text = render_clean_input(original_text, items, sections, warnings)
    cleaned_count = len(items)

    if cleaned_count == 0:
        raise ValueError("Reader could not identify any question item from input")

    result = {
        "original_input": str(input_path),
        "clean_input": str(clean_input_path),
        "original_detected_question_count": original_count,
        "cleaned_question_count": cleaned_count,
        "used_model_reader": used_model_reader,
        "model_expected_question_count": model_expected_question_count,
        "original_input_text": original_text,
        "sections": sections,
        "items": items,
        "warnings": warnings,
    }

    write_text_file(clean_input_path, cleaned_text)
    write_json_file(output_json_path, result)
    print(f"Reader 完成，整理后题目数：{cleaned_count}，可读版已写入：{clean_input_path}")
    return result


def main():
    if len(sys.argv) != 4:
        print("Usage: python reader_agent.py <input.md> <reader_result.json> <reader_clean_input.md>")
        raise SystemExit(1)
    run(sys.argv[1], sys.argv[2], sys.argv[3])


if __name__ == "__main__":
    main()
