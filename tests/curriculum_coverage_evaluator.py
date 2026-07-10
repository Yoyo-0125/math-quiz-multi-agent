import json
import re
from pathlib import Path

import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "codes"))

from budget import estimate_tokens


SAMPLE_DIR = PROJECT_ROOT / "examples" / "coverage_rounds"
REPORT_MD_PATH = PROJECT_ROOT / "docs" / "curriculum_coverage_report.md"
REPORT_JSON_PATH = PROJECT_ROOT / "docs" / "curriculum_coverage_report.json"

PER_ROUND_TOKEN_LIMIT = 580
PROJECT_RUNTIME_LIMIT_SECONDS = 360


TYPE_TARGETS = {
    "integer_and_radical_calculation": r"\|-?\d+\|.*\\sqrt",
    "algebraic_simplification": r"化简",
    "factorization": r"因式分解",
    "linear_equation": r"3\(x-2\)=2x\+5",
    "linear_inequality": r"2x-3\\le7",
    "system_of_equations": r"begin\{cases\}",
    "fractional_equation": r"\\frac\{2\}\{x-1\}=1",
    "quadratic_equation": r"x\^2-5x\+6=0",
    "linear_function": r"一次函数|直线过点",
    "inverse_proportion": r"反比例函数",
    "quadratic_function_vertex": r"顶点|对称轴",
    "percent_word_problem": r"打八折",
    "statistics_mean_median": r"平均数和中位数",
    "basic_probability": r"概率",
    "ratio_and_proportion": r"a:b=2:3|比例式",
    "sets": r"集合",
    "log_domain": r"\\log_2\(x-1\)",
    "exponential_equation": r"2\^x=8",
    "log_equation": r"\\log_3\(x\+1\)=2",
    "function_extreme": r"最小值",
    "trig_identity": r"sin\^2.*cos\^2",
    "trig_value": r"\\sin\\theta",
    "absolute_value_inequality": r"\|x-1\|<3",
    "arithmetic_sequence": r"等差数列",
    "geometric_sequence": r"等比数列",
    "series_sum": r"1\+2\+\\cdots\+20",
    "vectors": r"向量",
    "complex_numbers": r"i\)",
    "binomial_theorem": r"二项式定理",
    "analytic_line": r"斜率",
    "circle": r"圆 .*半径|圆心",
    "ellipse": r"椭圆",
    "combination_counting": r"选法数",
    "variance": r"方差",
    "derivative": r"导数",
    "tangent_slope": r"切线斜率",
}


DIFFICULTY_TARGETS = {
    "junior_basic": r"Round 01|Round 02",
    "senior_basic": r"Round 03|Round 04",
    "senior_comprehensive": r"Round 05|导数|椭圆|方差",
}


ANSWER_EXPECTATIONS = {
    "round_01_junior_algebra_core": ["-8", "2a^3b^2", "(x-2)(x-3)", "x=11", "x\\le5"],
    "round_02_junior_equations_ratio": ["x=3", "y=4", "x=2", "x=10", "58"],
    "round_03_junior_functions_data": ["y=2x+1", "k=12", "(2,-1)", "平均数 $4$", "\\frac35"],
    "round_04_high_functions": ["\\{2\\}", "(1,+\\infty)", "x=3", "x=8", "-1"],
    "round_05_high_trig_inequality": ["$1$", "\\frac45", "1<x<3", "-2<x<4"],
    "round_06_high_sequences_binomial": ["29", "48", "210", "$6$"],
    "round_07_high_vectors_complex": ["$1$", "$2$", "2i", "$5$"],
    "round_08_high_analytic_geometry": ["x=1", "$2$", "(1,-2)", "$3$"],
    "round_09_high_probability_calculus": ["$10$", "\\frac38", "\\frac23", "3x^2-3", "$2$"],
}


def read_text(path):
    return path.read_text(encoding="utf-8")


def normalize(text):
    return re.sub(r"\s+", "", text)


def sample_paths():
    return sorted(
        path for path in SAMPLE_DIR.glob("round_*.md")
        if not path.name.endswith("_answer_key.md")
    )


def count_questions(text):
    return len(re.findall(r"^\d+\.", text, re.MULTILINE))


def target_report(text, targets):
    covered = {
        name: bool(re.search(pattern, text, re.MULTILINE | re.DOTALL))
        for name, pattern in targets.items()
    }
    total = len(covered)
    hit = sum(1 for value in covered.values() if value)
    return {
        "covered": hit,
        "total": total,
        "ratio": round(hit / total, 4) if total else 1.0,
        "missing": [name for name, value in covered.items() if not value],
        "targets": covered,
    }


def check_answers(sample_path):
    stem = sample_path.stem
    answer_path = sample_path.with_name(f"{stem}_answer_key.md")
    answer_text = read_text(answer_path)
    normalized_answer = normalize(answer_text)
    expected = ANSWER_EXPECTATIONS.get(stem, [])
    missing = [
        item for item in expected
        if normalize(item) not in normalized_answer
    ]
    return {
        "answer_key": str(answer_path.relative_to(PROJECT_ROOT)),
        "expected_checks": len(expected),
        "missing_checks": missing,
        "passed": not missing,
    }


def build_report():
    samples = []
    all_text_parts = []
    total_questions = 0
    for path in sample_paths():
        text = read_text(path)
        all_text_parts.append(text)
        question_count = count_questions(text)
        total_questions += question_count
        token_count = estimate_tokens(text)
        samples.append(
            {
                "input": str(path.relative_to(PROJECT_ROOT)),
                "questions": question_count,
                "estimated_tokens": token_count,
                "within_token_limit": token_count <= PER_ROUND_TOKEN_LIMIT,
                "answer_quality": check_answers(path),
            }
        )

    all_text = "\n\n".join(all_text_parts)
    type_coverage = target_report(all_text, TYPE_TARGETS)
    difficulty_coverage = target_report(all_text, DIFFICULTY_TARGETS)
    wrong_answer_files = [
        sample["input"]
        for sample in samples
        if not sample["answer_quality"]["passed"]
    ]
    over_token_files = [
        sample["input"]
        for sample in samples
        if not sample["within_token_limit"]
    ]
    return {
        "sample_dir": str(SAMPLE_DIR.relative_to(PROJECT_ROOT)),
        "sample_count": len(samples),
        "total_questions": total_questions,
        "per_round_token_limit": PER_ROUND_TOKEN_LIMIT,
        "project_runtime_limit_seconds": PROJECT_RUNTIME_LIMIT_SECONDS,
        "samples": samples,
        "type_coverage": type_coverage,
        "difficulty_coverage": difficulty_coverage,
        "answer_quality": {
            "files_checked": len(samples),
            "wrong_files": wrong_answer_files,
            "error_rate": round(len(wrong_answer_files) / len(samples), 4) if samples else 0,
            "target_error_rate": 0.05,
            "passes_error_target": len(wrong_answer_files) / len(samples) <= 0.05 if samples else True,
        },
        "over_token_files": over_token_files,
    }


def write_markdown(report):
    missing_type = ", ".join(report["type_coverage"]["missing"]) or "none"
    missing_diff = ", ".join(report["difficulty_coverage"]["missing"]) or "none"
    over_token = ", ".join(report["over_token_files"]) or "none"
    wrong_files = ", ".join(report["answer_quality"]["wrong_files"]) or "none"
    sample_lines = [
        f"- {sample['input']}: questions={sample['questions']}, tokens={sample['estimated_tokens']}, within_limit={sample['within_token_limit']}"
        for sample in report["samples"]
    ]
    content = "\n".join(
        [
            "Curriculum Coverage Report",
            "",
            f"sample_dir: {report['sample_dir']}",
            f"sample_count: {report['sample_count']}",
            f"total_questions: {report['total_questions']}",
            f"per_round_token_limit: {report['per_round_token_limit']}",
            f"project_runtime_limit_seconds: {report['project_runtime_limit_seconds']}",
            "",
            f"type_coverage: {report['type_coverage']['covered']}/{report['type_coverage']['total']} = {report['type_coverage']['ratio'] * 100:.1f}%",
            f"missing_type: {missing_type}",
            f"difficulty_coverage: {report['difficulty_coverage']['covered']}/{report['difficulty_coverage']['total']} = {report['difficulty_coverage']['ratio'] * 100:.1f}%",
            f"missing_difficulty: {missing_diff}",
            f"answer_error_rate: {report['answer_quality']['error_rate'] * 100:.1f}%",
            f"wrong_answer_files: {wrong_files}",
            f"over_token_files: {over_token}",
            "",
            "samples:",
            *sample_lines,
            "",
            "Note: runtime must be checked by tests/project_runtime_benchmark.py because it measures the actual pipeline command.",
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

    failures = []
    if report["type_coverage"]["ratio"] < 1.0:
        failures.append("type coverage incomplete")
    if report["difficulty_coverage"]["ratio"] < 1.0:
        failures.append("difficulty coverage incomplete")
    if report["answer_quality"]["error_rate"] > 0.05:
        failures.append("answer error rate too high")
    if report["over_token_files"]:
        failures.append("some rounds exceed token limit")
    if failures:
        raise SystemExit("; ".join(failures))


if __name__ == "__main__":
    main()
