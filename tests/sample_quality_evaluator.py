import json
import math
import time
from pathlib import Path

import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "codes"))

from budget import estimate_tokens


INPUT_PATH = PROJECT_ROOT / "examples" / "input.md"
ANSWER_KEY_PATH = PROJECT_ROOT / "examples" / "input_answer_key.md"
REPORT_PATH = PROJECT_ROOT / "docs" / "sample_quality_report.md"
REPORT_JSON_PATH = PROJECT_ROOT / "docs" / "sample_quality_report.json"

BASELINE_INPUT_TOKEN_LIMIT = 580
MAX_SECONDS = 360


TYPE_TARGETS = {
    "constant_quadratic": ["1.1"],
    "ordered_roots_parameter": ["1.2"],
    "factor_parameter_quadratic": ["1.3"],
    "discriminant_positive_zero_negative": ["1.4", "1.5"],
    "leading_coefficient_sign_change": ["1.6"],
    "known_solution_parameter": ["2"],
    "rational_parameter_denominator": ["2.2", "2.4"],
    "rational_parameter_numerator": ["2.5", "2.6", "v1", "v2"],
    "variant_boundary": ["v1", "v2"],
}

DIFFICULTY_TARGETS = {
    "easy": ["1.1", "1.2"],
    "medium": ["1.3", "1.4", "1.5", "2", "2.1", "2.2", "2.3"],
    "hard_high_school": ["1.6", "2.4", "2.5", "2.6", "v1", "v2"],
}


def outside(x, left, right, left_closed=False, right_closed=False):
    left_ok = x <= left if left_closed else x < left
    right_ok = x >= right if right_closed else x > right
    return left_ok or right_ok


def between(x, left, right, left_closed=False, right_closed=False):
    left_ok = x >= left if left_closed else x > left
    right_ok = x <= right if right_closed else x < right
    return left_ok and right_ok


def safe_eval(value_func, *args):
    try:
        return value_func(*args)
    except ZeroDivisionError:
        return None


def truth_1_1(x):
    return x * x - 5 * x + 6 > 0


def ans_1_1(x):
    return x < 2 or x > 3


def truth_1_2(a, b, x):
    return (x - a) * (x - b) < 0


def ans_1_2(a, b, x):
    return a < x < b


def truth_1_3(a, x):
    return x * x + 2 * a * x - 8 * a * a > 0


def ans_1_3(a, x):
    if a > 0:
        return x < -4 * a or x > 2 * a
    if a == 0:
        return abs(x) > 1e-9
    return x < 2 * a or x > -4 * a


def truth_1_4(a, x):
    return 2 * x * x + a * x + 2 > 0


def ans_1_4(a, x):
    if -4 < a < 4:
        return True
    if abs(a) == 4:
        return abs(x + a / 4) > 1e-9
    left = (-a - math.sqrt(a * a - 16)) / 4
    right = (-a + math.sqrt(a * a - 16)) / 4
    return outside(x, left, right)


def truth_1_5(a, x):
    return x * x - a * x + 4 < 0


def ans_1_5(a, x):
    if -4 <= a <= 4:
        return False
    left = (a - math.sqrt(a * a - 16)) / 2
    right = (a + math.sqrt(a * a - 16)) / 2
    return between(x, left, right)


def truth_1_6(a, x):
    return (a - 1) * x * x + 3 * x + 1 >= 0


def ans_1_6(a, x):
    if a < 1:
        left = (-3 + math.sqrt(13 - 4 * a)) / (2 * (a - 1))
        right = (-3 - math.sqrt(13 - 4 * a)) / (2 * (a - 1))
        return between(x, left, right, True, True)
    if a == 1:
        return x >= -1 / 3
    if 1 < a < 13 / 4:
        left = (-3 - math.sqrt(13 - 4 * a)) / (2 * (a - 1))
        right = (-3 + math.sqrt(13 - 4 * a)) / (2 * (a - 1))
        return outside(x, left, right, True, True)
    return True


def truth_2(x):
    return -10 * x * x - 7 * x - 1 > 0


def ans_2(x):
    return -1 / 2 < x < -1 / 5


def truth_2_1(a, x):
    denominator = 4 * x - 5
    if abs(denominator) < 1e-9:
        return False
    return (x + a) / denominator <= 1 / 2


def ans_2_1(a, x):
    if abs(x - 5 / 4) < 1e-9:
        return False
    root = (2 * a + 5) / 2
    if a < -5 / 4:
        return x <= root or x > 5 / 4
    if a == -5 / 4:
        return abs(x - 5 / 4) > 1e-9
    return x < 5 / 4 or x >= root


def truth_2_2(a, x):
    denominator = 2 * x - a
    if abs(denominator) < 1e-9:
        return False
    return (3 * x + 1) / denominator > -1


def ans_2_2(a, x):
    if abs(x - a / 2) < 1e-9:
        return False
    root = (a - 1) / 5
    pole = a / 2
    if a < -2 / 3:
        return x < pole or x > root
    if a == -2 / 3:
        return abs(x + 1 / 3) > 1e-9
    return x < root or x > pole


def truth_2_3(m, x):
    denominator = 5 * x - 3
    if abs(denominator) < 1e-9:
        return False
    return (2 * x - m) / denominator > -2


def ans_2_3(m, x):
    if abs(x - 3 / 5) < 1e-9:
        return False
    root = (m + 6) / 12
    if m < 6 / 5:
        return x < root or x > 3 / 5
    if m == 6 / 5:
        return abs(x - 3 / 5) > 1e-9
    return x < 3 / 5 or x > root


def truth_2_4(m, x):
    denominator = 4 * x - m
    if abs(denominator) < 1e-9:
        return False
    return (2 - x) / denominator <= 1 / 2


def ans_2_4(m, x):
    if abs(x - m / 4) < 1e-9:
        return False
    root = (m + 4) / 6
    if m < 8:
        return x < m / 4 or x >= root
    if m == 8:
        return abs(x - 2) > 1e-9
    return x <= root or x > m / 4


def truth_2_5(a, x):
    denominator = 2 * x + 3
    if abs(denominator) < 1e-9:
        return False
    return (a * x - 1) / denominator >= 0


def ans_2_5(a, x):
    if abs(x + 3 / 2) < 1e-9:
        return False
    if a == 0:
        return x < -3 / 2
    if a > 0:
        return x < -3 / 2 or x >= 1 / a
    if a < -2 / 3:
        return -3 / 2 < x <= 1 / a
    if a == -2 / 3:
        return False
    return 1 / a <= x < -3 / 2


def truth_2_6(m, x):
    denominator = 4 * x + 1
    if abs(denominator) < 1e-9:
        return False
    return (m * x - 2) / denominator < 1


def ans_2_6(m, x):
    if abs(x + 1 / 4) < 1e-9:
        return False
    if m > 4:
        return -1 / 4 < x < 3 / (m - 4)
    if m == 4:
        return x > -1 / 4
    if -8 < m < 4:
        return x < 3 / (m - 4) or x > -1 / 4
    if m == -8:
        return abs(x + 1 / 4) > 1e-9
    return x < -1 / 4 or x > 3 / (m - 4)


def truth_v1(m, x):
    denominator = 2 * x - 3
    if abs(denominator) < 1e-9:
        return False
    return (m * x + 5) / denominator <= -1


def ans_v1(m, x):
    if abs(x - 3 / 2) < 1e-9:
        return False
    if m < -10 / 3:
        return x <= -2 / (m + 2) or x > 3 / 2
    if m == -10 / 3:
        return abs(x - 3 / 2) > 1e-9
    if -10 / 3 < m < -2:
        return x < 3 / 2 or x >= -2 / (m + 2)
    if m == -2:
        return x < 3 / 2
    return -2 / (m + 2) <= x < 3 / 2


def truth_v2(a, x):
    denominator = 3 * x + 1
    if abs(denominator) < 1e-9:
        return False
    return (a * x - 2) / denominator > -1


def ans_v2(a, x):
    if abs(x + 1 / 3) < 1e-9:
        return False
    if a < -6:
        return -1 / 3 < x < 1 / (a + 3)
    if a == -6:
        return False
    if -6 < a < -3:
        return 1 / (a + 3) < x < -1 / 3
    if a == -3:
        return x < -1 / 3
    return x < -1 / 3 or x > 1 / (a + 3)


ITEM_TESTS = {
    "1.1": [(truth_1_1, ans_1_1, [(-1,), (0,), (2,), (2.5,), (4,)])],
    "1.2": [(truth_1_2, ans_1_2, [(1, 3, 0), (1, 3, 2), (1, 3, 4)])],
    "1.3": [(truth_1_3, ans_1_3, [(2, -9), (2, 0), (-2, -5), (-2, 0), (0, 0), (0, 1)])],
    "1.4": [(truth_1_4, ans_1_4, [(0, 0), (4, -1), (5, -2), (5, 0), (-5, 0), (-5, 2)])],
    "1.5": [(truth_1_5, ans_1_5, [(0, 0), (4, 2), (5, 1), (5, 3), (-5, -3), (-5, 0)])],
    "1.6": [(truth_1_6, ans_1_6, [(0, -1), (0, 0), (1, -1), (1, 0), (2, -5), (2, 0), (4, 0)])],
    "2": [(truth_2, ans_2, [(-1,), (-0.4,), (-0.1,)])],
    "2.1": [(truth_2_1, ans_2_1, [(-2, 0), (-2, 1), (0, 1), (0, 3), (2, 1), (2, 5)])],
    "2.2": [(truth_2_2, ans_2_2, [(-1, -1), (-1, 0), (0, -1), (0, 1), (1, 0), (1, 2)])],
    "2.3": [(truth_2_3, ans_2_3, [(0, 0), (0, 1), (2, 0), (2, 1), (-2, -1), (-2, 1)])],
    "2.4": [(truth_2_4, ans_2_4, [(0, -1), (0, 1), (8, 1), (8, 3), (12, 2), (12, 4)])],
    "2.5": [(truth_2_5, ans_2_5, [(0, -2), (0, 0), (1, -2), (1, 2), (-1, -2), (-1, -1), (-1, 0)])],
    "2.6": [(truth_2_6, ans_2_6, [(5, 0), (5, 4), (4, 0), (0, -2), (0, 0), (-10, -1), (-10, 0)])],
    "v1": [(truth_v1, ans_v1, [(-4, 0), (-4, 2), (-3, 0), (-2, 0), (-1, 0), (-1, 2)])],
    "v2": [(truth_v2, ans_v2, [(-7, -1), (-7, 0), (-5, -1), (-5, 0), (-3, -1), (-2, -1), (-2, 1)])],
}


def evaluate_answer_correctness():
    item_results = {}
    for item_id, tests in ITEM_TESTS.items():
        errors = []
        checks = 0
        for truth_func, answer_func, samples in tests:
            for sample in samples:
                truth = truth_func(*sample)
                answer = answer_func(*sample)
                checks += 1
                if truth != answer:
                    errors.append(
                        {
                            "sample": sample,
                            "truth": truth,
                            "answer": answer,
                        }
                    )
        item_results[item_id] = {
            "checks": checks,
            "errors": errors,
            "passed": len(errors) == 0,
        }
    wrong_items = [item for item, data in item_results.items() if not data["passed"]]
    return item_results, wrong_items


def coverage_ratio(targets):
    total = len(targets)
    covered = sum(1 for items in targets.values() if items)
    return {
        "covered": covered,
        "total": total,
        "ratio": round(covered / total, 4),
        "missing": [name for name, items in targets.items() if not items],
    }


def write_report(report):
    content = "\n\n".join(
        [
            "Sample Quality Report",
            f"type coverage: {report['type_coverage']['covered']}/{report['type_coverage']['total']} = {report['type_coverage']['ratio'] * 100:.1f}%",
            f"difficulty coverage: {report['difficulty_coverage']['covered']}/{report['difficulty_coverage']['total']} = {report['difficulty_coverage']['ratio'] * 100:.1f}%",
            f"estimated input tokens: {report['efficiency']['estimated_input_tokens']} / {report['efficiency']['baseline_input_token_limit']}",
            f"evaluation time seconds: {report['efficiency']['evaluation_seconds']} / {report['efficiency']['max_seconds']}",
            f"answer error rate: {report['answer_quality']['wrong_count']}/{report['answer_quality']['total_count']} = {report['answer_quality']['error_rate'] * 100:.1f}%",
            "wrong items: " + (", ".join(report["answer_quality"]["wrong_items"]) or "none"),
        ]
    )
    REPORT_PATH.write_text(content + "\n", encoding="utf-8")


def main():
    started = time.perf_counter()
    input_text = INPUT_PATH.read_text(encoding="utf-8")
    ANSWER_KEY_PATH.read_text(encoding="utf-8")
    item_results, wrong_items = evaluate_answer_correctness()
    elapsed = round(time.perf_counter() - started, 4)

    estimated_input_tokens = estimate_tokens(input_text)
    total_items = len(ITEM_TESTS)
    wrong_count = len(wrong_items)
    report = {
        "input": str(INPUT_PATH.relative_to(PROJECT_ROOT)),
        "answer_key": str(ANSWER_KEY_PATH.relative_to(PROJECT_ROOT)),
        "type_coverage": coverage_ratio(TYPE_TARGETS),
        "difficulty_coverage": coverage_ratio(DIFFICULTY_TARGETS),
        "efficiency": {
            "estimated_input_tokens": estimated_input_tokens,
            "baseline_input_token_limit": BASELINE_INPUT_TOKEN_LIMIT,
            "within_token_limit": estimated_input_tokens <= BASELINE_INPUT_TOKEN_LIMIT,
            "evaluation_seconds": elapsed,
            "max_seconds": MAX_SECONDS,
            "within_time_limit": elapsed <= MAX_SECONDS,
        },
        "answer_quality": {
            "total_count": total_items,
            "wrong_count": wrong_count,
            "correct_count": total_items - wrong_count,
            "error_rate": round(wrong_count / total_items, 4),
            "target_error_rate": 0.1,
            "passes_error_target": wrong_count / total_items < 0.1,
            "wrong_items": wrong_items,
            "item_results": item_results,
        },
    }
    REPORT_JSON_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    write_report(report)
    print(json.dumps(report, ensure_ascii=False, indent=2))

    if not report["efficiency"]["within_token_limit"]:
        raise SystemExit("sample exceeds baseline input token limit")
    if not report["efficiency"]["within_time_limit"]:
        raise SystemExit("sample evaluation exceeds time limit")
    if not report["answer_quality"]["passes_error_target"]:
        raise SystemExit("answer error rate is too high")
    if report["type_coverage"]["ratio"] < 1.0:
        raise SystemExit("type coverage incomplete")
    if report["difficulty_coverage"]["ratio"] < 1.0:
        raise SystemExit("difficulty coverage incomplete")


if __name__ == "__main__":
    main()
