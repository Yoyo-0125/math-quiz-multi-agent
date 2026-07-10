import re


class ValidationError(ValueError):
    pass


def coerce_int(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit():
            return int(stripped)
        try:
            parsed = float(stripped)
        except ValueError:
            return value
        if parsed.is_integer():
            return int(parsed)
    return value


def normalize_string_list(data, key):
    if isinstance(data.get(key), str):
        data[key] = [data[key]]


def normalize_score(data, key):
    data[key] = coerce_int(data.get(key))


def require_keys(data, keys, label):
    if not isinstance(data, dict):
        raise ValidationError(f"{label} must be a JSON object")
    missing = [key for key in keys if key not in data]
    if missing:
        raise ValidationError(f"{label} missing required keys: {', '.join(missing)}")


def require_type(data, key, expected_type, label):
    if not isinstance(data.get(key), expected_type):
        raise ValidationError(f"{label}.{key} must be {expected_type.__name__}")


def require_string_list(data, key, label):
    value = data.get(key)
    if not isinstance(value, list):
        raise ValidationError(f"{label}.{key} must be list")
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise ValidationError(f"{label}.{key}[{index}] must be str")


def require_score(data, key, label):
    normalize_score(data, key)
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 100:
        raise ValidationError(f"{label}.{key} must be an integer from 0 to 100")


def normalize_decomposer_result(data):
    for key in [
        "subtopics",
        "question_types",
        "style_features",
        "generation_constraints",
        "uncertain_points",
    ]:
        normalize_string_list(data, key)
    distribution = data.get("difficulty_distribution")
    if isinstance(distribution, dict):
        for key in ["easy", "medium", "hard"]:
            if key in distribution:
                distribution[key] = coerce_int(distribution[key])


def normalize_reviewer_result(data):
    normalize_score(data, "score")
    for key in ["major_issues", "minor_issues", "missing_points"]:
        normalize_string_list(data, key)


def normalize_generator_result(data):
    for key in ["generation_notes", "possible_risks"]:
        normalize_string_list(data, key)


def normalize_qc_result(data):
    normalize_score(data, "score")
    normalize_string_list(data, "revision_instructions")


def has_major_issue(data):
    for key in ["question_issues", "answer_issues", "style_issues"]:
        for item in data.get(key, []) or []:
            if isinstance(item, dict) and item.get("severity") == "major":
                return True
    return False


def reject_self_admitted_generator_errors(data):
    combined_text = "\n".join(
        [
            data.get("answer_key_markdown", ""),
            "\n".join(data.get("possible_risks", [])),
        ]
    )
    blocked_phrases = [
        "\u6211\u5199\u6210",  # wo xie cheng
        "\u5ffd\u7565",
        "\u4e0d\u5b8c\u6574",
        "\u5e94\u52a0\u4e0a",
        "\u9700\u8981\u8fdb\u4e00\u6b65",
        "\u7b54\u6848\u9519\u8bef",
        "\u89e3\u96c6\u9519\u8bef",
        "\u4e0d\u786e\u5b9a",
        "\u672a\u5b8c\u6210",
        "\u5e94\u4fee\u6b63",
    ]
    for phrase in blocked_phrases:
        if phrase in combined_text:
            raise ValidationError(
                "generator output admits an unresolved answer error: "
                f"{phrase}"
            )


def validate_decomposer_result(data):
    label = "decomposer"
    normalize_decomposer_result(data)
    require_keys(
        data,
        [
            "topic",
            "subtopics",
            "grade_level",
            "question_types",
            "format_structure",
            "difficulty_distribution",
            "style_features",
            "latex_usage",
            "generation_constraints",
            "uncertain_points",
        ],
        label,
    )
    for key in ["topic", "grade_level", "format_structure", "latex_usage"]:
        require_type(data, key, str, label)
    for key in [
        "subtopics",
        "question_types",
        "style_features",
        "generation_constraints",
        "uncertain_points",
    ]:
        require_string_list(data, key, label)

    distribution = data["difficulty_distribution"]
    require_keys(distribution, ["easy", "medium", "hard"], f"{label}.difficulty_distribution")
    for key in ["easy", "medium", "hard"]:
        value = distribution.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise ValidationError(
                f"{label}.difficulty_distribution.{key} must be a non-negative number"
            )

    if "detected_question_count" in data:
        detected = data["detected_question_count"]
        if isinstance(detected, bool) or not isinstance(detected, int) or detected < 1:
            raise ValidationError("decomposer.detected_question_count must be a positive int")

    if "source_structure" in data:
        source_structure = data["source_structure"]
        require_type(data, "source_structure", dict, label)
        total = source_structure.get("total_question_count")
        if isinstance(total, bool) or not isinstance(total, int) or total < 1:
            raise ValidationError(
                "decomposer.source_structure.total_question_count must be a positive int"
            )
        items = source_structure.get("items")
        if items is not None and not isinstance(items, list):
            raise ValidationError("decomposer.source_structure.items must be list")


def validate_reviewer_result(data):
    label = "reviewer"
    normalize_reviewer_result(data)
    require_keys(
        data,
        [
            "is_valid",
            "score",
            "major_issues",
            "minor_issues",
            "missing_points",
            "suggested_corrections",
            "review_summary",
        ],
        label,
    )
    require_type(data, "is_valid", bool, label)
    require_score(data, "score", label)
    for key in ["major_issues", "minor_issues", "missing_points"]:
        require_string_list(data, key, label)
    require_type(data, "suggested_corrections", dict, label)
    require_type(data, "review_summary", str, label)


def validate_generator_result(data):
    label = "generator"
    normalize_generator_result(data)
    require_keys(
        data,
        [
            "questions_markdown",
            "answer_key_markdown",
            "generation_notes",
            "possible_risks",
        ],
        label,
    )
    require_type(data, "questions_markdown", str, label)
    require_type(data, "answer_key_markdown", str, label)
    require_string_list(data, "generation_notes", label)
    require_string_list(data, "possible_risks", label)
    if not data["questions_markdown"].strip():
        raise ValidationError("generator.questions_markdown must not be empty")
    if not data["answer_key_markdown"].strip():
        raise ValidationError("generator.answer_key_markdown must not be empty")
    reject_self_admitted_generator_errors(data)


def validate_issue_list(items, label):
    if not isinstance(items, list):
        raise ValidationError(f"{label} must be list")
    for index, item in enumerate(items):
        item_label = f"{label}[{index}]"
        if not isinstance(item, dict):
            raise ValidationError(f"{item_label} must be object")
        if item.get("severity") not in ["major", "minor"]:
            raise ValidationError(f"{item_label}.severity must be major or minor")
        for key in ["question_id", "problem", "suggested_fix"]:
            if key in item and not isinstance(item[key], str):
                raise ValidationError(f"{item_label}.{key} must be str")


def validate_qc_result(data):
    label = "qc"
    normalize_qc_result(data)
    require_keys(
        data,
        [
            "is_passed",
            "score",
            "question_issues",
            "answer_issues",
            "style_issues",
            "revision_instructions",
            "qc_summary",
        ],
        label,
    )
    require_type(data, "is_passed", bool, label)
    require_score(data, "score", label)
    validate_issue_list(data["question_issues"], "qc.question_issues")
    validate_issue_list(data["answer_issues"], "qc.answer_issues")
    validate_issue_list(data["style_issues"], "qc.style_issues")
    require_string_list(data, "revision_instructions", label)
    require_type(data, "qc_summary", str, label)

    if has_major_issue(data):
        data["is_passed"] = False
        if isinstance(data.get("score"), int) and data["score"] >= 90:
            data["score"] = 89


def strip_markdown_prefix(line):
    stripped = line.strip()
    stripped = re.sub(r"^[\s>*#\-\+]+", "", stripped)
    return stripped.strip("*_ ")


def count_variant_markers(text):
    return len(
        re.findall(r"(?:\u53d8\u5f0f|Variant)\s*\d+", text, re.IGNORECASE)
    )


def count_subquestion_markers(text):
    return len(re.findall(r"[\(\uFF08]\s*\d+\s*[\)\uFF09]", text))


def is_top_level_line(text):
    return re.match(r"^\d+\s*[.:\)]", text) is not None


def has_math_relation(text):
    return any(marker in text for marker in ["<", ">", "=", "\\le", "\\ge", "\\neq"])


def next_meaningful_line(lines, start_index):
    for following_line in lines[start_index:]:
        following = strip_markdown_prefix(following_line)
        if following and not following.startswith("#"):
            return following
    return ""


def count_question_items(markdown_text):
    subquestion_count = 0
    variant_count = 0
    top_level_count = 0
    lines = markdown_text.splitlines()

    for index, line in enumerate(lines):
        normalized = strip_markdown_prefix(line)
        if not normalized or normalized.startswith("#"):
            continue

        variants = count_variant_markers(normalized)
        if variants:
            variant_count += variants
            continue

        subquestions = count_subquestion_markers(normalized)
        if subquestions:
            subquestion_count += subquestions
            continue

        if is_top_level_line(normalized):
            following = next_meaningful_line(lines, index + 1)
            if count_subquestion_markers(following) or count_variant_markers(following):
                continue
            top_level_count += 1
            continue

    if subquestion_count or variant_count:
        return subquestion_count + variant_count + top_level_count
    return top_level_count


def count_numbered_items(markdown_text):
    return count_question_items(markdown_text)


def validate_question_answer_pair(
    questions_markdown,
    answer_key_markdown,
    expected_question_count=None,
):
    question_count = count_question_items(questions_markdown)
    answer_count = count_question_items(answer_key_markdown)

    if expected_question_count is not None:
        expected_question_count = int(expected_question_count)
        if question_count != expected_question_count:
            raise ValidationError(
                "generated question count does not match target: "
                f"{question_count} questions, expected {expected_question_count}"
            )
        if answer_count != expected_question_count:
            raise ValidationError(
                "answer count does not match target: "
                f"{answer_count} answers, expected {expected_question_count}"
            )

    if question_count == 0 or answer_count == 0:
        print(
            "Warning: could not reliably count numbered questions or answers; "
            "leaving this check to QC."
        )
        return

    if question_count != answer_count:
        raise ValidationError(
            "question and answer counts do not match: "
            f"{question_count} questions, {answer_count} answers"
        )
