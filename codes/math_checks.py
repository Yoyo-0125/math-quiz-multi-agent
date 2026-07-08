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
