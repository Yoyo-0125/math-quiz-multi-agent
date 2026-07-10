import json
import math
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "codes"))

from decomposer_agent import analyze_input_structure
from reader_agent import extract_reader_items
from math_checks import validate_math_audit
from validators import (
    ValidationError,
    count_question_items,
    validate_qc_result,
    validate_question_answer_pair,
)


CASES = [
    ("case_01_flat_numbered.md", 3),
    ("case_02_nested_subquestions.md", 3),
    ("case_03_title_plus_application.md", 2),
    ("case_04_variants.md", 3),
    ("case_05_mixed_sections.md", 6),
    ("case_06_no_markdown_plain.md", 3),
    ("case_07_edge_spacing.md", 3),
]


def assert_equal(actual, expected, label):
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected}, got {actual}")


def test_question_counting():
    base = PROJECT_ROOT / "examples" / "test_cases"
    for filename, expected in CASES:
        text = (base / filename).read_text(encoding="utf-8")
        counted = count_question_items(text)
        structure = analyze_input_structure(text)
        assert_equal(counted, expected, f"{filename} validators count")
        assert_equal(
            structure["total_question_count"],
            expected,
            f"{filename} decomposer structure count",
        )


def test_reader_normalizes_split_heading_question():
    text = "\n".join(
        [
            "# worksheet",
            "## section",
            "**1. Solve the inequalities**",
            "* **(1)** $x^2-1>0$",
            "* **(2)** $x^2-4<0$",
            "**2. Application**",
            "* Given the solution set of $x^2-ax-b<0$ is $2<x<3$, solve $bx^2-ax-1>0$.",
            "### variants",
            "* **Variant 1:** $\\frac{mx+5}{2x-3}\\le -1$",
        ]
    )
    _, items, _, warnings = extract_reader_items(text)
    assert_equal(len(items), 4, "reader normalized item count")
    assert "Application" in items[2]["text"], "reader should merge heading with following math line"
    assert not warnings, f"reader should not leave unresolved headings: {warnings}"


def test_answer_pair_counting():
    questions = "\n".join(
        [
            "1. $x^2-1>0$",
            "2. $x^2-4\\le0$",
            "3. $\\frac{x-1}{x+2}>0$",
        ]
    )
    answers = "\n".join(
        [
            "1. $x<-1$ or $x>1$",
            "2. $-2\\le x\\le2$",
            "3. $x<-2$ or $x>1$",
        ]
    )
    validate_question_answer_pair(questions, answers, expected_question_count=3)

    bad_answers = "1. $x<-1$ or $x>1$\n2. $-2\\le x\\le2$"
    try:
        validate_question_answer_pair(questions, bad_answers, expected_question_count=3)
    except ValidationError:
        return
    raise AssertionError("answer pair validator did not reject missing answer")


def test_qc_major_issue_normalization():
    data = {
        "is_passed": True,
        "score": 95,
        "question_issues": [
            {
                "question_id": "6",
                "severity": "major",
                "problem": "wrong answer",
                "suggested_fix": "fix it",
            }
        ],
        "answer_issues": [],
        "style_issues": [],
        "revision_instructions": [],
        "qc_summary": "bad",
    }
    validate_qc_result(data)
    assert_equal(data["is_passed"], False, "major QC issue should fail")
    assert data["score"] < 90, "major QC issue should lower pass score"


def q6_value(k, x):
    return (k - 2) * x * x + 4 * x + 1


def q6_solution_contains(k, x):
    if k < 2:
        left = (-2 + math.sqrt(6 - k)) / (k - 2)
        right = (-2 - math.sqrt(6 - k)) / (k - 2)
        return left <= x <= right
    if k == 2:
        return x >= -0.25
    if 2 < k < 6:
        left = (-2 - math.sqrt(6 - k)) / (k - 2)
        right = (-2 + math.sqrt(6 - k)) / (k - 2)
        return x <= left or x >= right
    return True


def test_q6_regression_samples():
    samples = [
        (1, [-1, 0, 2, 5]),
        (2, [-1, -0.25, 0]),
        (3, [-5, -2, 0, 1]),
        (6, [-10, 0, 10]),
        (7, [-10, 0, 10]),
    ]
    for k, xs in samples:
        for x in xs:
            expected = q6_value(k, x) >= -1e-9
            actual = q6_solution_contains(k, x)
            assert_equal(actual, expected, f"q6 k={k}, x={x}")


def q11_value(n, x):
    if abs(5 * x - n) < 1e-9:
        return None
    return (3 - x) / (5 * x - n) <= 0.2


def q11_solution_contains(n, x):
    if abs(x - n / 5) < 1e-9:
        return False
    if n > 15:
        return x <= (n + 15) / 10 or x > n / 5
    if n == 15:
        return abs(x - 3) > 1e-9
    return x < n / 5 or x >= (n + 15) / 10


def test_q11_regression_samples():
    samples = [
        (10, [0, 2, 2.5, 3, 4]),
        (15, [2, 3, 4]),
        (20, [2, 3.5, 4, 5]),
    ]
    for n, xs in samples:
        for x in xs:
            expected = q11_value(n, x)
            if expected is None:
                expected = False
            actual = q11_solution_contains(n, x)
            assert_equal(actual, expected, f"q11 n={n}, x={x}")


def test_final_outputs_basic_health():
    output_dir = PROJECT_ROOT / "outputs"
    questions_path = output_dir / "generated_questions_final.md"
    answers_path = output_dir / "answer_key_final.md"
    qc_path = output_dir / "qc_final.json"
    if not questions_path.exists() or not answers_path.exists() or not qc_path.exists():
        return

    questions = questions_path.read_text(encoding="utf-8")
    answers = answers_path.read_text(encoding="utf-8")
    validate_question_answer_pair(questions, answers, expected_question_count=15)

    blocks = {
        int(match.group(1)): match.group(0)
        for match in re.finditer(r"(?ms)^(\d+)\. .*?(?=^\d+\. |\Z)", answers)
    }
    for number in [6, 11]:
        block = blocks[number]
        if "?" in block:
            raise AssertionError(f"final answer block {number} contains question marks")

    qc = json.loads(qc_path.read_text(encoding="utf-8"))
    validate_qc_result(qc)
    assert_equal(qc["is_passed"], True, "final QC status")


def test_math_audit_blocks_unsafe_rational_answer():
    questions = "1. $\\frac{3-x}{5x-n}\\le\\frac12$"
    unsafe_answer = "\n".join(
        [
            "**(1)**",
            "- $n<15$: $x<\\frac n5$ or $x\\ge\\frac{n+6}{7}$.",
            "- $n=15$: $x\\ne3$.",
            "- $n>15$: $x\\le\\frac{n+6}{7}$ or $x>\\frac n5$.",
        ]
    )
    try:
        validate_math_audit(questions, unsafe_answer)
    except ValidationError:
        return
    raise AssertionError("math audit did not reject unsafe rational answer format")


def test_math_audit_accepts_guarded_rational_answer():
    questions = "1. $\\frac{3-x}{5x-n}\\le\\frac12$"
    guarded_answer = "\n".join(
        [
            "**(1)**",
            "Equivalent form: $\\dfrac{n+6-7x}{5x-n}\\le0$, with $x\\ne\\dfrac n5$.",
            "- $n<15$: $x<\\dfrac n5$ or $x\\ge\\dfrac{n+6}{7}$.",
            "- $n=15$: $x\\ne3$.",
            "- $n>15$: $x\\le\\dfrac{n+6}{7}$ or $x>\\dfrac n5$.",
        ]
    )
    validate_math_audit(questions, guarded_answer)


def test_math_audit_blocks_wrong_outside_between_shape():
    questions = "1. $\\frac{mx+5}{2x-3}\\le -1$"
    wrong_answer = "\n".join(
        [
            "**Variant 1**",
            "Equivalent form: $\\dfrac{(m+2)x+2}{2x-3}\\le0$, with $x\\ne\\dfrac32$.",
            "- $m<-\\frac{10}{3}$: $-\\dfrac{2}{m+2}\\le x<\\dfrac32$.",
            "- $m=-\\frac{10}{3}$: $x\\ne\\dfrac32$.",
            "- $-\\frac{10}{3}<m<-2$: $-\\dfrac{2}{m+2}\\le x<\\dfrac32$.",
            "- $m=-2$: $x<\\dfrac32$.",
            "- $m>-2$: $x\\le-\\dfrac{2}{m+2}$ or $x>\\dfrac32$.",
        ]
    )
    try:
        validate_math_audit(questions, wrong_answer)
    except ValidationError:
        return
    raise AssertionError("math audit did not reject reversed outside/between cases")


def test_math_audit_accepts_correct_outside_between_shape():
    questions = "1. $\\frac{mx+5}{2x-3}\\le -1$"
    correct_answer = "\n".join(
        [
            "**Variant 1**",
            "Equivalent form: $\\dfrac{(m+2)x+2}{2x-3}\\le0$, with $x\\ne\\dfrac32$.",
            "- $m<-\\frac{10}{3}$: $x\\le-\\dfrac{2}{m+2}$ or $x>\\dfrac32$.",
            "- $m=-\\frac{10}{3}$: $x\\ne\\dfrac32$.",
            "- $-\\frac{10}{3}<m<-2$: $x<\\dfrac32$ or $x\\ge-\\dfrac{2}{m+2}$.",
            "- $m=-2$: $x<\\dfrac32$.",
            "- $m>-2$: $-\\dfrac{2}{m+2}\\le x<\\dfrac32$.",
        ]
    )
    validate_math_audit(questions, correct_answer)


def main():
    test_question_counting()
    test_reader_normalizes_split_heading_question()
    test_answer_pair_counting()
    test_qc_major_issue_normalization()
    test_q6_regression_samples()
    test_q11_regression_samples()
    test_final_outputs_basic_health()
    test_math_audit_blocks_unsafe_rational_answer()
    test_math_audit_accepts_guarded_rational_answer()
    test_math_audit_blocks_wrong_outside_between_shape()
    test_math_audit_accepts_correct_outside_between_shape()
    print(json.dumps({"status": "ok", "cases": len(CASES)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
