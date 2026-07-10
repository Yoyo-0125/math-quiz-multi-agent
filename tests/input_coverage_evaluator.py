import json
import re
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INPUT_PATH = PROJECT_ROOT / "examples" / "input.md"
REPORT_MD_PATH = PROJECT_ROOT / "docs" / "input_coverage_report.md"
REPORT_JSON_PATH = PROJECT_ROOT / "docs" / "input_coverage_report.json"


LINE_TARGETS = {
    "section_heading": r"^##\s+",
    "top_level_question": r"^\*\*\d+\.",
    "subquestion": r"\*\*[\(\uFF08]\d+[\)\uFF09]\*\*",
    "application_line": r"\u5df2\u77e5.*\u89e3\u96c6",
    "rational_inequality": r"\\frac",
    "quadratic_inequality": r"x\^2",
    "variant": r"\u53d8\u5f0f\s*\d+",
    "parameter_constraint": r"\\quad\s*\([^)]*[<>=][^)]*\)",
}


CONDITION_TARGETS = {
    "strict_greater": r">\s*0",
    "strict_less": r"<\s*0",
    "non_strict_ge": r"\\ge\s*0|\u2265\s*0",
    "non_strict_le": r"\\le|\u2264",
    "fixed_root_order_condition": r"a\s*<\s*b|m\s*<\s*n",
    "parameter_zero_case_needed": r"\([a-zA-Z]\s*-\s*\d+\)x\^2",
    "discriminant_positive_zero_negative": (
        r"2x\^2\s*\+\s*[a-zA-Z]x\s*\+\s*2|x\^2\s*-\s*[a-zA-Z]x\s*\+\s*4"
    ),
    "rational_denominator_exclusion": r"\\frac\{[^{}]+\}\{[^{}]*x[^{}]*\}",
    "rational_root_order_swap": (
        r"\\frac\{[a-zA-Z]x\s*[+-]\s*\d+\}\{\d+x\s*[+-]\s*\d+\}|"
        r"\\frac\{[a-zA-Z]x\s*\+\s*\d+\}\{\d+x\s*-\s*\d+\}"
    ),
    "known_solution_parameter_solving": (
        r"\u5df2\u77e5.*\u89e3\u96c6.*\u6c42"
    ),
}


BRANCH_TARGETS = {
    "quadratic_factor_parameter_order": (
        r"x\^2\s*\+\s*2ax\s*-\s*8a\^2|x\^2\s*\+\s*\(a\^2\+a\)x\s*\+\s*a\^3"
    ),
    "quadratic_fixed_order": r"\(x-a\)\(x-b\)\s*<\s*0",
    "quadratic_discriminant_all_real_or_none": (
        r"2x\^2\s*\+\s*ax\s*\+\s*2|x\^2\s*-\s*ax\s*\+\s*4"
    ),
    "quadratic_leading_coefficient_sign_change": r"\(a-1\)x\^2",
    "known_interval_vieta_branch": r"x\^2\s*-\s*[a-zA-Z]x\s*-\s*[a-zA-Z]\s*<\s*0",
    "rational_strict_branch": r">\s*-1|<\s*1",
    "rational_non_strict_branch": r"\\le|\\ge",
    "rational_parameter_in_denominator": (
        r"\{[^{}]*x-[a-zA-Z]\}|\{[a-zA-Z]x[+-]\d+\}"
    ),
    "rational_parameter_in_numerator": r"\{[a-zA-Z]x[+-]\d+\}",
    "variant_boundary_branch": r"\u53d8\u5f0f\s*1|\u53d8\u5f0f\s*2",
}


def covered_targets(text, targets):
    return {
        name: bool(re.search(pattern, text, re.MULTILINE))
        for name, pattern in targets.items()
    }


def ratio(result):
    total = len(result)
    covered = sum(1 for value in result.values() if value)
    return {
        "covered": covered,
        "total": total,
        "ratio": round(covered / total, 4) if total else 1.0,
        "missing": [name for name, value in result.items() if not value],
    }


def count_questions(text):
    subquestions = len(re.findall(r"\*\*[\(\uFF08]\d+[\)\uFF09]\*\*", text))
    variants = len(re.findall(r"\u53d8\u5f0f\s*\d+", text))
    application = len(
        re.findall(r"^\*\s+\u5df2\u77e5.*\u6c42", text, re.MULTILINE)
    )
    return subquestions + variants + application


def build_report():
    text = INPUT_PATH.read_text(encoding="utf-8")
    line = covered_targets(text, LINE_TARGETS)
    condition = covered_targets(text, CONDITION_TARGETS)
    branch = covered_targets(text, BRANCH_TARGETS)
    return {
        "input": str(INPUT_PATH.relative_to(PROJECT_ROOT)),
        "question_count": count_questions(text),
        "line_coverage": ratio(line),
        "condition_coverage": ratio(condition),
        "branch_coverage": ratio(branch),
        "line_targets": line,
        "condition_targets": condition,
        "branch_targets": branch,
    }


def write_markdown(report):
    def section(title, key):
        data = report[key]
        missing = ", ".join(data["missing"]) if data["missing"] else "none"
        return "\n".join(
            [
                f"{title}: {data['covered']}/{data['total']} = "
                f"{data['ratio'] * 100:.1f}%",
                f"missing: {missing}",
            ]
        )

    content = "\n\n".join(
        [
            "Input Coverage Report",
            f"input: {report['input']}",
            f"question_count: {report['question_count']}",
            section("line coverage", "line_coverage"),
            section("condition coverage", "condition_coverage"),
            section("branch coverage", "branch_coverage"),
            (
                "Note: this evaluates whether the input sample covers key "
                "worksheet structures, condition combinations, and math branches. "
                "It is not Python source-code execution coverage."
            ),
        ]
    )
    REPORT_MD_PATH.write_text(content + "\n", encoding="utf-8")


def main():
    report = build_report()
    REPORT_JSON_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    write_markdown(report)
    print(json.dumps(report, ensure_ascii=False, indent=2))

    failed = [
        name
        for name in ["line_coverage", "condition_coverage", "branch_coverage"]
        if report[name]["ratio"] < 1.0
    ]
    if failed:
        raise SystemExit("coverage incomplete: " + ", ".join(failed))


if __name__ == "__main__":
    main()
