import re

from validators import ValidationError


def _compact_math(text):
    return re.sub(r"\s+", "", text)


def _normalize_latex_expr(expr):
    expr = expr.replace("\\left", "").replace("\\right", "")
    expr = expr.replace("^", "**")
    expr = expr.replace("{", "(").replace("}", ")")
    expr = expr.replace("\\cdot", "*")
    expr = expr.replace(" ", "")
    return expr


def _extract_first_dollar_expr(text):
    matches = re.findall(r"\$(.+?)\$", text)
    if not matches:
        return None
    for match in matches:
        if "x" in match and ("<" in match or ">" in match or "\\le" in match or "\\ge" in match):
            return match
    return matches[0]


def _split_inequality(expr):
    for operator in ["\\le", "\\ge", "<=", ">=", "<", ">"]:
        if operator in expr:
            left, right = expr.split(operator, 1)
            return left, operator, right
    return None, None, None


def _extract_factor_product(answer_text):
    matches = re.findall(r"\(([^\(\)]*x[^\(\)]*)\)\s*\(([^\(\)]*x[^\(\)]*)\)", answer_text)
    if not matches:
        return None
    return matches[0]


def _parse_linear_factor(factor):
    factor = _normalize_latex_expr(factor)
    factor = factor.replace(" ", "")
    if factor == "x":
        return 1, "0"
    if factor.startswith("x+"):
        return 1, factor[2:]
    if factor.startswith("x-"):
        return 1, "-" + factor[2:]
    return None


def _strip_outer_parens(expr):
    expr = expr.strip()
    if expr.startswith("(") and expr.endswith(")"):
        return expr[1:-1]
    return expr


def _normalize_term(term):
    term = _strip_outer_parens(_normalize_latex_expr(term))
    term = term.replace("*", "")
    return term


def _negate_term(term):
    term = _normalize_term(term)
    if term.startswith("-"):
        return term[1:]
    return "-" + term


def _sum_terms(first, second):
    first = _normalize_term(first)
    second = _normalize_term(second)
    if first == "0":
        return second
    if second == "0":
        return first
    if first.startswith("-") and not second.startswith("-"):
        return f"{second}{first}"
    if second.startswith("-"):
        return f"{first}{second}"
    return f"{first}+{second}"


def _product_terms(first, second):
    first = _normalize_term(first)
    second = _normalize_term(second)
    if first == "0" or second == "0":
        return "0"
    sign = ""
    if first.startswith("-"):
        sign = "-" if sign == "" else ""
        first = first[1:]
    if second.startswith("-"):
        sign = "-" if sign == "" else ""
        second = second[1:]
    if first == "1":
        return sign + second
    if second == "1":
        return sign + first
    return sign + first + second


def _expected_middle_and_constant(left_factor, right_factor):
    left = _parse_linear_factor(left_factor)
    right = _parse_linear_factor(right_factor)
    if left is None or right is None:
        return None

    _, b = left
    _, d = right
    return _sum_terms(b, d), _product_terms(b, d)


def _parse_simple_question_quadratic(question_left):
    left = _compact_math(_normalize_latex_expr(question_left)).replace("*", "")
    left = left.replace(" ", "")
    match = re.match(r"^x\*\*2(.+?)x(.+)$", left)
    if not match:
        match = re.match(r"^x\^2(.+?)x(.+)$", left)
    if not match:
        left = left.replace("x**2", "x^2")
        match = re.match(r"^x\^2(.+?)x(.+)$", left)
    if not match:
        return None
    return _normalize_term(match.group(1)), _normalize_term(match.group(2))


def _looks_like_same_simple_quadratic(question_expr, factors):
    left, operator, right = _split_inequality(question_expr)
    if operator is None:
        return True
    if _compact_math(right) not in ["0", "0.0"]:
        return True

    normalized_left = _normalize_latex_expr(left)
    normalized_factors = _normalize_latex_expr(f"({factors[0]})({factors[1]})")

    # Fast exact-ish check for cases where the model repeats the product form.
    if _compact_math(normalized_factors) in _compact_math(normalized_left):
        return True

    expected = _expected_middle_and_constant(factors[0], factors[1])
    actual = _parse_simple_question_quadratic(left)
    if expected is None or actual is None:
        return True

    return expected == actual


def validate_basic_math_consistency(questions_markdown, answer_key_markdown):
    question_expr = _extract_first_dollar_expr(questions_markdown)
    factors = _extract_factor_product(answer_key_markdown)
    if question_expr is None or factors is None:
        return

    if not _looks_like_same_simple_quadratic(question_expr, factors):
        raise ValidationError(
            "basic math consistency check failed: answer factorization does not match question expression"
        )


HIGH_RISK_PATTERNS = [
    r"\([a-zA-Z]-\d+\)x\^2",
    r"\\frac\{[^{}]*x[^{}]*\}\{[^{}]*x[^{}]*\}",
    r"/\s*\([^)]*x[^)]*\)",
]


def split_answer_blocks(answer_key_markdown):
    blocks = []
    current_title = None
    current_lines = []

    for line in answer_key_markdown.splitlines():
        stripped = line.strip()
        is_answer_heading = (
            stripped.startswith("**")
            and stripped.endswith("**")
            and (
                re.search(r"\d+", stripped)
                or "Variant" in stripped
                or "\u53d8\u5f0f" in stripped
            )
        )
        if is_answer_heading:
            if current_title is not None:
                blocks.append((current_title, "\n".join(current_lines).strip()))
            current_title = stripped
            current_lines = [line]
        elif current_title is not None:
            current_lines.append(line)

    if current_title is not None:
        blocks.append((current_title, "\n".join(current_lines).strip()))
    return blocks


def contains_any(text, needles):
    return any(needle in text for needle in needles)


def _parse_latex_number(text):
    value = text.strip().replace(" ", "")
    value = value.replace("\\dfrac", "\\frac")
    sign = -1 if value.startswith("-") else 1
    if value.startswith(("-", "+")):
        value = value[1:]
    match = re.fullmatch(r"\\frac\{?(-?\d+)\}?\{?(-?\d+)\}?", value)
    if match:
        return sign * int(match.group(1)) / int(match.group(2))
    return sign * float(value)


def _format_param_linear_root(param, offset, const):
    # Root of (param+offset)x+const is -const/(param+offset).
    if offset == 0:
        denominator = param
    elif offset > 0:
        denominator = f"{param}+{offset}"
    else:
        denominator = f"{param}{offset}"
    return f"-{const}/({denominator})"


def _normalize_equivalent_expr(expr):
    return (
        expr.replace(" ", "")
        .replace("\\left", "")
        .replace("\\right", "")
        .replace("\\dfrac", "\\frac")
        .replace("{", "")
        .replace("}", "")
    )


def _parse_param_linear_numerator(expr):
    normalized = _normalize_equivalent_expr(expr)
    match = re.fullmatch(
        r"\(([a-zA-Z])([+-]\d+)?\)x([+-]\d+)", normalized
    )
    if not match:
        return None
    param = match.group(1)
    offset = int(match.group(2) or "0")
    const = int(match.group(3))
    return param, offset, const


def _parse_numeric_linear_denominator(expr):
    normalized = _normalize_equivalent_expr(expr)
    match = re.fullmatch(r"([+-]?\d*)x([+-]\d+)", normalized)
    if not match:
        return None
    coefficient_text = match.group(1)
    if coefficient_text in ["", "+"]:
        coefficient = 1
    elif coefficient_text == "-":
        coefficient = -1
    else:
        coefficient = int(coefficient_text)
    const = int(match.group(2))
    return coefficient, const


def _parse_equivalent_fraction(block):
    compact = block.replace(" ", "")
    match = re.search(
        r"\\d?frac\{(.+?)\}\{(.+?)\}(\\le|\\ge|<|>)0",
        compact,
    )
    if not match:
        return None
    numerator = _parse_param_linear_numerator(match.group(1))
    denominator = _parse_numeric_linear_denominator(match.group(2))
    if numerator is None or denominator is None:
        return None
    return numerator, denominator, match.group(3)


def _extract_case_condition(line):
    match = re.search(r"\$([^$]+)\$", line)
    return match.group(1).replace(" ", "") if match else None


def _sample_value_for_condition(condition, param):
    condition = condition.replace("\\dfrac", "\\frac")
    escaped_param = re.escape(param)

    match = re.fullmatch(rf"{escaped_param}<(.+)", condition)
    if match:
        return _parse_latex_number(match.group(1)) - 1

    match = re.fullmatch(rf"{escaped_param}>(.+)", condition)
    if match:
        return _parse_latex_number(match.group(1)) + 1

    match = re.fullmatch(rf"{escaped_param}=(.+)", condition)
    if match:
        return _parse_latex_number(match.group(1))

    match = re.fullmatch(rf"(.+)<{escaped_param}<(.+)", condition)
    if match:
        left = _parse_latex_number(match.group(1))
        right = _parse_latex_number(match.group(2))
        return (left + right) / 2

    return None


def _expected_rational_shape(sample, numerator, denominator, operator):
    _, offset, const = numerator
    denominator_coefficient, denominator_const = denominator
    if abs(sample + offset) < 1e-9:
        return "special"
    numerator_root = -const / (sample + offset)
    denominator_root = -denominator_const / denominator_coefficient
    if abs(numerator_root - denominator_root) < 1e-9:
        return "special"

    leading_sign = (sample + offset) / denominator_coefficient
    wants_nonpositive = operator in ["\\le", "<"]
    if leading_sign == 0:
        return "special"
    between_when_positive = wants_nonpositive
    if leading_sign > 0:
        return "between" if between_when_positive else "outside"
    return "outside" if between_when_positive else "between"


def _line_uses_outside_union(line):
    return " or " in line or "\u6216" in line


def audit_rational_case_shapes(title, block):
    parsed = _parse_equivalent_fraction(block)
    if parsed is None:
        return []

    numerator, denominator, operator = parsed
    param = numerator[0]
    issues = []
    for raw_line in block.splitlines():
        stripped = raw_line.strip()
        if not stripped.startswith("-"):
            continue
        condition = _extract_case_condition(stripped)
        if not condition:
            continue
        sample = _sample_value_for_condition(condition, param)
        if sample is None:
            continue
        expected_shape = _expected_rational_shape(
            sample, numerator, denominator, operator
        )
        if expected_shape == "special":
            continue
        uses_outside = _line_uses_outside_union(stripped)
        if expected_shape == "outside" and not uses_outside:
            issues.append(
                f"{title} case {condition} should be an outside union, but answer is not written with or/或"
            )
        if expected_shape == "between" and uses_outside:
            issues.append(
                f"{title} case {condition} should be a between interval, but answer is written as an outside union"
            )
    return issues


def audit_high_risk_answer_format(questions_markdown, answer_key_markdown):
    """Catch recurring high-risk answer patterns before trusting model QC.

    This is deliberately conservative: it does not try to solve every generated
    inequality. It blocks the formats that repeatedly caused wrong answers unless
    the answer includes the algebraic guardrails needed for review and repair.
    """
    issues = []
    answer_blocks = split_answer_blocks(answer_key_markdown)

    for title, block in answer_blocks:
        compact = block.replace(" ", "")
        is_first_section_subquestion = title.startswith("**1.")
        is_variant = "Variant" in title or "\u53d8\u5f0f" in title
        is_plain_parenthesized = re.match(r"^\*\*\(\d+\)\*\*", title) is not None
        has_fractional_form = "\\dfrac" in block or "\\frac" in block
        has_parameter_cases = len(re.findall(r"\$[a-zA-Z][<=>]", block)) >= 2

        if (
            has_fractional_form
            and has_parameter_cases
            and not is_first_section_subquestion
            and (is_variant or is_plain_parenthesized)
        ):
            if not contains_any(block, ["Equivalent form", "\u7b49\u4ef7", "\u5316\u4e3a"]):
                issues.append(
                    f"{title} rational inequality answer must include an equivalent single-fraction form"
                )
            if not contains_any(compact, ["x\\ne", "x\\neq", "x!="]):
                issues.append(
                    f"{title} rational inequality answer must explicitly exclude denominator zero"
                )
            issues.extend(audit_rational_case_shapes(title, block))

        if ("x^2" in block or "x\\^2" in block) and has_parameter_cases:
            if contains_any(block, ["<2", ">2", "<6", ">6"]):
                if not contains_any(block, ["Check:", "\u68c0\u9a8c", "sample"]):
                    issues.append(
                        f"{title} parameterized quadratic answer must include a sample check for sign-changing leading coefficient"
                    )

    if issues:
        raise ValidationError("math audit failed: " + "; ".join(issues))


def validate_math_audit(questions_markdown, answer_key_markdown):
    validate_basic_math_consistency(questions_markdown, answer_key_markdown)
    audit_high_risk_answer_format(questions_markdown, answer_key_markdown)
