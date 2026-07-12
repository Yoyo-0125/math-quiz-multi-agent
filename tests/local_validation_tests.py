import json
import importlib.util
import math
import re
import shutil
import sys
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "codes"))

from decomposer_agent import analyze_input_structure
from reader_agent import extract_reader_items
from math_latex_sanitizer import sanitize_math_markdown
from math_checks import validate_math_audit
from ocr_agent import build_question_tree, parse_paddleocr_jsonl
from question_bank import (
    enrich_question,
    render_similar_markdown,
    similar_questions,
)
from qc_agent import (
    apply_originality_report,
    apply_question_completeness_audit,
    apply_recurrence_answer_audit,
    build_originality_report,
)
from result_store import (
    decomposer_fingerprint,
    restore_recent_result,
    restore_weighted_verified_items,
    save_success_record,
    save_verified_items,
)
from run_pipeline import (
    PAUSE_FILENAME,
    PipelineQualityError,
    combine_itemwise_outputs,
    ensure_no_excessive_exact_duplicates,
    parse_selected_item_ids,
    wait_if_paused,
)
from time_machine import scan_time_machine
from validators import (
    ValidationError,
    count_question_items,
    validate_qc_result,
    validate_question_answer_pair,
)


def load_professional_module():
    module_path = PROJECT_ROOT / "专业模式" / "professional_pipeline.py"
    spec = importlib.util.spec_from_file_location("professional_pipeline", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


def test_reader_merges_choices_and_drops_solution_fragments():
    text = "\n".join(
        [
            "1. 已知函数 $y=x^2-2x$，求顶点坐标。",
            "A. $(1,-1)$",
            "B. $(2,0)$",
            "代入得 $x=1$",
            "综上，答案为 A",
            "2. 解不等式 $x^2-1>0$。",
        ]
    )
    _, items, _, _ = extract_reader_items(text)
    assert_equal(len(items), 2, "reader should keep only two real questions")
    assert "A." in items[0]["text"] and "B." in items[0]["text"], "choices should attach to previous question"
    assert "综上" not in "\n".join(item["text"] for item in items), "solution fragments should be removed"


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
    final_question_count = count_question_items(questions)
    try:
        validate_question_answer_pair(
            questions,
            answers,
            expected_question_count=final_question_count,
        )
    except ValidationError:
        return

    blocks = {
        int(match.group(1)): match.group(0)
        for match in re.finditer(r"(?ms)^(\d+)\. .*?(?=^\d+\. |\Z)", answers)
    }
    for number in [6, 11]:
        if number not in blocks:
            return
        block = blocks[number]
        if "?" in block:
            raise AssertionError(f"final answer block {number} contains question marks")

    qc = json.loads(qc_path.read_text(encoding="utf-8"))
    validate_qc_result(qc)
    if qc.get("is_passed") is not True:
        return
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


def test_originality_report_flags_near_copy():
    decomposer = {
        "source_structure": {
            "items": [
                {
                    "id": 1,
                    "text": "1. 解不等式 $x^2 - 5x + 6 > 0$",
                }
            ]
        }
    }
    copied = "1. 解不等式 $x^2 - 5x + 6 > 0$"
    report = build_originality_report(decomposer, copied)
    assert report["high_similarity_pairs"], "near-copy question should be flagged"

    result = {
        "is_passed": True,
        "score": 95,
        "question_issues": [],
        "answer_issues": [],
        "style_issues": [],
        "revision_instructions": [],
        "qc_summary": "ok",
    }
    apply_originality_report(result, report)
    assert_equal(result["is_passed"], False, "originality failure should fail QC")
    assert result["question_issues"], "originality failure should add a question issue"


def base_qc_result():
    return {
        "is_passed": True,
        "score": 95,
        "question_issues": [],
        "answer_issues": [],
        "style_issues": [],
        "revision_instructions": [],
        "qc_summary": "ok",
    }


def test_qc_blocks_incomplete_question_stem():
    result = base_qc_result()
    apply_question_completeness_audit(result, "1. $$f(x)=2x+\\frac{1}{x}$$")
    assert_equal(result["is_passed"], False, "bare formula question should fail")
    assert result["question_issues"], "bare formula should add question issue"

    good = base_qc_result()
    apply_question_completeness_audit(good, "1. 讨论函数 $f(x)=2x+\\frac{1}{x}$ 的单调性。")
    assert_equal(good["is_passed"], True, "explicit task question should pass completeness audit")


def test_qc_blocks_wrong_recurrence_closed_form():
    result = base_qc_result()
    questions = "1. 求递推数列的通项公式：$a_1=2,\\ a_{n+1}=3a_n+2$。"
    answers = "1. 最终答案：$a_n=2\\cdot 3^{n-1}-1$"
    apply_recurrence_answer_audit(result, questions, answers)
    assert_equal(result["is_passed"], False, "wrong recurrence formula should fail")
    assert result["answer_issues"], "wrong recurrence should add answer issue"


def test_result_store_save_and_restore():
    base = PROJECT_ROOT / "outputs" / "tmp_result_store_test"
    source = base / "source"
    target = base / "target"
    result_root = base / "result"
    if base.exists():
        shutil.rmtree(base)
    source.mkdir(parents=True)
    target.mkdir(parents=True)

    decomposer = {
        "topic": "二次不等式",
        "grade_level": "高一",
        "question_types": ["解不等式"],
        "detected_question_count": 1,
        "source_structure": {"total_question_count": 1, "items": []},
    }
    (source / "decomposer_final.json").write_text(
        json.dumps(decomposer, ensure_ascii=False),
        encoding="utf-8",
    )
    (source / "reviewer_final.json").write_text("{}", encoding="utf-8")
    (source / "generator_final.json").write_text("{}", encoding="utf-8")
    (source / "qc_final.json").write_text("{}", encoding="utf-8")
    (source / "generated_questions_final.md").write_text("1. test question", encoding="utf-8")
    (source / "answer_key_final.md").write_text("1. test answer", encoding="utf-8")

    record = save_success_record(source, result_root=result_root)
    assert record and record.exists(), "result record should be created"
    status = restore_recent_result(target, result_root=result_root)
    assert status, "result record should restore"
    assert_equal(
        (target / "generated_questions_final.md").read_text(encoding="utf-8"),
        "1. test question",
        "restored question content",
    )


def test_result_store_can_disable_global_fallback_for_itemwise_runs():
    base = PROJECT_ROOT / "outputs" / "tmp_no_global_fallback_test"
    source = base / "source"
    target = base / "target"
    result_root = base / "result"
    if base.exists():
        shutil.rmtree(base)
    source.mkdir(parents=True)
    target.mkdir(parents=True)

    source_decomposer = {
        "topic": "old successful topic",
        "grade_level": "高一",
        "question_types": ["集合"],
        "detected_question_count": 3,
        "source_structure": {"total_question_count": 3, "items": []},
    }
    target_decomposer = {
        "topic": "current failed single item",
        "grade_level": "高一",
        "question_types": ["函数"],
        "detected_question_count": 1,
        "source_structure": {"total_question_count": 1, "items": [{"id": 1, "text": "current"}]},
    }
    (source / "decomposer_final.json").write_text(json.dumps(source_decomposer, ensure_ascii=False), encoding="utf-8")
    (source / "reviewer_final.json").write_text("{}", encoding="utf-8")
    (source / "generator_final.json").write_text("{}", encoding="utf-8")
    (source / "qc_final.json").write_text("{}", encoding="utf-8")
    (source / "generated_questions_final.md").write_text("1. old question", encoding="utf-8")
    (source / "answer_key_final.md").write_text("1. old answer", encoding="utf-8")
    (target / "decomposer_final.json").write_text(json.dumps(target_decomposer, ensure_ascii=False), encoding="utf-8")

    save_success_record(source, result_root=result_root)
    status = restore_recent_result(target, result_root=result_root, allow_global_record=False)
    assert_equal(status, None, "itemwise fallback should not restore unrelated global record")
    assert not (target / "generated_questions_final.md").exists(), "unrelated global fallback should not write questions"


def test_result_store_saves_qc_verified_items_only():
    base = PROJECT_ROOT / "outputs" / "tmp_verified_items_test"
    if base.exists():
        shutil.rmtree(base)
    result_root = base / "result"
    decomposer = {
        "topic": "二次不等式",
        "grade_level": "高一",
        "question_types": ["解不等式"],
        "detected_question_count": 3,
    }
    questions = "\n".join(
        [
            "1. question one",
            "2. question two",
            "3. question three",
        ]
    )
    answers = "\n".join(
        [
            "1. answer one",
            "2. answer two",
            "3. answer three",
        ]
    )
    qc = {
        "is_passed": False,
        "score": 80,
        "question_issues": [
            {
                "question_id": "2",
                "severity": "major",
                "problem": "bad",
                "suggested_fix": "fix",
            }
        ],
        "answer_issues": [],
    }
    saved = save_verified_items(decomposer, questions, answers, qc, result_root=result_root)
    assert_equal(len(saved), 2, "verified item count")
    saved_questions = sorted(path.joinpath("question.md").read_text(encoding="utf-8").strip() for path in saved)
    assert_equal(saved_questions, ["1. question one", "3. question three"], "saved verified questions")


def test_result_store_groups_exact_decomposer_and_restores_weighted_items():
    base = PROJECT_ROOT / "outputs" / "tmp_weighted_verified_items_test"
    if base.exists():
        shutil.rmtree(base)
    result_root = base / "result"
    output_dir = base / "target"
    output_dir.mkdir(parents=True)
    decomposer = {
        "topic": "二次不等式",
        "grade_level": "高一",
        "question_types": ["解不等式"],
        "format_structure": "单题",
        "detected_question_count": 1,
        "source_structure": {"total_question_count": 1, "items": [{"id": 1, "text": "source"}]},
    }
    qc_low = {"is_passed": False, "score": 75, "question_issues": [], "answer_issues": []}
    qc_high = {"is_passed": True, "score": 96, "question_issues": [], "answer_issues": []}

    saved_low = save_verified_items(
        decomposer,
        "1. low question",
        "1. low answer",
        qc_low,
        result_root=result_root,
    )
    saved_high = save_verified_items(
        decomposer,
        "1. high question",
        "1. high answer",
        qc_high,
        result_root=result_root,
    )
    assert saved_low and saved_high, "both verified runs should save"
    assert_equal(
        saved_low[0].parents[2],
        saved_high[0].parents[2],
        "same exact decomposer should share one fingerprint directory",
    )
    assert decomposer_fingerprint(decomposer) in str(saved_low[0]), "fingerprint should be in path"

    status = restore_weighted_verified_items(output_dir, decomposer, result_root=result_root)
    assert status, "weighted verified items should restore"
    restored_question = (output_dir / "generated_questions_final.md").read_text(encoding="utf-8")
    restored_answer = (output_dir / "answer_key_final.md").read_text(encoding="utf-8")
    assert restored_question.startswith("1. "), "restored question should be renumbered"
    assert restored_answer.startswith("1. "), "restored answer should be renumbered"


def test_itemwise_outputs_combine_with_global_numbering():
    base = PROJECT_ROOT / "outputs" / "tmp_itemwise_combine_test"
    if base.exists():
        shutil.rmtree(base)
    item1 = base / "items" / "item_001"
    item2 = base / "items" / "item_002"
    item1.mkdir(parents=True)
    item2.mkdir(parents=True)

    for item_dir, label in [(item1, "alpha"), (item2, "beta")]:
        (item_dir / "generated_questions_final.md").write_text(
            f"1. {label} question",
            encoding="utf-8",
        )
        (item_dir / "answer_key_final.md").write_text(
            f"1. {label} answer",
            encoding="utf-8",
        )
        (item_dir / "qc_final.json").write_text(
            json.dumps({"is_passed": True, "score": 95}, ensure_ascii=False),
            encoding="utf-8",
        )
        (item_dir / "generator_final.json").write_text("{}", encoding="utf-8")
        (item_dir / "decomposer_final.json").write_text("{}", encoding="utf-8")
        (item_dir / "reviewer_final.json").write_text("{}", encoding="utf-8")

    combine_itemwise_outputs(
        base,
        [
            {
                "questions": str(item1 / "generated_questions_final.md"),
                "answer_key": str(item1 / "answer_key_final.md"),
                "qc": str(item1 / "qc_final.json"),
                "generator": str(item1 / "generator_final.json"),
                "decomposer": str(item1 / "decomposer_final.json"),
                "reviewer": str(item1 / "reviewer_final.json"),
            },
            {
                "questions": str(item2 / "generated_questions_final.md"),
                "answer_key": str(item2 / "answer_key_final.md"),
                "qc": str(item2 / "qc_final.json"),
                "generator": str(item2 / "generator_final.json"),
                "decomposer": str(item2 / "decomposer_final.json"),
                "reviewer": str(item2 / "reviewer_final.json"),
            },
        ],
    )
    questions = (base / "generated_questions_final.md").read_text(encoding="utf-8")
    answers = (base / "answer_key_final.md").read_text(encoding="utf-8")
    assert "1. alpha question" in questions
    assert "2. beta question" in questions
    assert "1. alpha answer" in answers
    assert "2. beta answer" in answers


def test_itemwise_combine_rejects_excessive_exact_duplicates():
    duplicated = [
        "1. 已知集合 $A=\\{x|x^2-(a+3)x+3a\\le0\\}$，若 $A\\subseteq[2,5]$，求 $a$。",
        "2. 已知集合 $A=\\{x|x^2-(a+3)x+3a\\le0\\}$，若 $A\\subseteq[2,5]$，求 $a$。",
        "3. 已知集合 $A=\\{x|x^2-(a+3)x+3a\\le0\\}$，若 $A\\subseteq[2,5]$，求 $a$。",
    ]
    try:
        ensure_no_excessive_exact_duplicates(duplicated)
    except PipelineQualityError:
        return
    raise AssertionError("duplicate guard did not reject repeated itemwise output")


def test_selected_item_id_parser():
    assert_equal(parse_selected_item_ids("1, 3, 5-7"), [1, 3, 5, 6, 7], "selected item range parser")
    assert_equal(parse_selected_item_ids(["2", "4-5"]), [2, 4, 5], "selected item list parser")


def test_pipeline_pause_flag_waits_until_resume():
    base = PROJECT_ROOT / "outputs" / "tmp_pause_flag_test"
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True)
    pause_path = base / PAUSE_FILENAME
    pause_path.write_text("paused\n", encoding="utf-8")

    def resume_soon():
        time.sleep(0.1)
        pause_path.unlink()

    thread = threading.Thread(target=resume_soon)
    thread.start()
    started = time.time()
    wait_if_paused(base, "test")
    thread.join(timeout=2)
    assert time.time() - started >= 0.08, "pause wait should block until flag is removed"


def test_professional_selector_prefers_passed_high_score_candidate():
    pro = load_professional_module()
    base = PROJECT_ROOT / "outputs" / "tmp_professional_selector_test"
    if base.exists():
        shutil.rmtree(base)
    passed_dir = base / "passed"
    failed_dir = base / "failed"
    passed_dir.mkdir(parents=True)
    failed_dir.mkdir(parents=True)

    (passed_dir / "qc_final.json").write_text(
        json.dumps(
            {
                "is_passed": True,
                "score": 96,
                "question_issues": [],
                "answer_issues": [],
                "style_issues": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (failed_dir / "qc_final.json").write_text(
        json.dumps(
            {
                "is_passed": False,
                "score": 98,
                "question_issues": [
                    {
                        "severity": "major",
                        "problem": "bad",
                        "suggested_fix": "fix",
                    }
                ],
                "answer_issues": [],
                "style_issues": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    best, ranked = pro.select_best_candidate(
        [
            {
                "candidate_index": 1,
                "status": "ok",
                "qc": str(failed_dir / "qc_final.json"),
            },
            {
                "candidate_index": 2,
                "status": "ok",
                "qc": str(passed_dir / "qc_final.json"),
            },
        ],
        selector_min_score=95,
    )
    assert_equal(best["candidate_index"], 2, "professional selector best candidate")
    assert_equal(ranked[0]["candidate_index"], 2, "professional selector ranking")


def test_professional_selector_penalizes_duplicate_questions():
    pro = load_professional_module()
    base = PROJECT_ROOT / "outputs" / "tmp_professional_duplicate_selector_test"
    if base.exists():
        shutil.rmtree(base)
    duplicate_dir = base / "duplicate"
    fresh_dir = base / "fresh"
    duplicate_dir.mkdir(parents=True)
    fresh_dir.mkdir(parents=True)

    qc_payload = {
        "is_passed": True,
        "score": 96,
        "question_issues": [],
        "answer_issues": [],
        "style_issues": [],
    }
    for item_dir in [duplicate_dir, fresh_dir]:
        (item_dir / "qc_final.json").write_text(
            json.dumps(qc_payload, ensure_ascii=False),
            encoding="utf-8",
        )

    repeated_question = "1. 解不等式 $x^2-5x+6>0$。"
    (duplicate_dir / "generated_questions_final.md").write_text(
        repeated_question,
        encoding="utf-8",
    )
    (fresh_dir / "generated_questions_final.md").write_text(
        "1. 已知二次函数 $f(x)=x^2-4x+1$，求 $f(x)>0$ 的解集。",
        encoding="utf-8",
    )

    best, _ = pro.select_best_candidate(
        [
            {
                "candidate_index": 1,
                "status": "ok",
                "qc": str(duplicate_dir / "qc_final.json"),
                "questions": str(duplicate_dir / "generated_questions_final.md"),
            },
            {
                "candidate_index": 2,
                "status": "ok",
                "qc": str(fresh_dir / "qc_final.json"),
                "questions": str(fresh_dir / "generated_questions_final.md"),
            },
        ],
        selector_min_score=95,
        previous_selected_texts=[repeated_question],
        duplicate_similarity_threshold=0.88,
    )
    assert_equal(best["candidate_index"], 2, "duplicate candidate should be penalized")


def test_time_machine_scans_replay_nodes():
    base = PROJECT_ROOT / "outputs" / "tmp_time_machine_test"
    if base.exists():
        shutil.rmtree(base)
    run_dir = base / "outputs"
    run_dir.mkdir(parents=True)
    (run_dir / "reader_result.json").write_text(
        json.dumps({"items": [{"id": 1, "text": "x^2>1"}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    (run_dir / "decomposer_final.json").write_text(
        json.dumps(
            {
                "topic": "quadratic inequality",
                "grade_level": "high school",
                "question_types": ["inequality"],
                "detected_question_count": 1,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (run_dir / "generated_questions_final.md").write_text(
        "1. Solve $x^2-4>0$.",
        encoding="utf-8",
    )
    (run_dir / "answer_key_final.md").write_text(
        "1. $x<-2$ or $x>2$.",
        encoding="utf-8",
    )
    (run_dir / "qc_final.json").write_text(
        json.dumps(
            {
                "is_passed": True,
                "score": 96,
                "question_issues": [],
                "answer_issues": [],
                "style_issues": [],
                "revision_instructions": [],
                "qc_summary": "ok",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    data = scan_time_machine(base, output_dirs=["outputs"], result_root="result")
    assert_equal(data["count"], 1, "time machine run count")
    stages = [node["stage"] for node in data["runs"][0]["nodes"]]
    assert "Reader" in stages
    assert "Decomposer" in stages
    assert "Generator" in stages
    assert "QC" in stages


def test_paddleocr_jsonl_parsing_and_question_tree():
    page_payload = {
        "result": {
            "layoutParsingResults": [
                {
                    "page_width": 1000,
                    "page_height": 2000,
                    "markdown": {
                        "text": "一、选择题\n1. 已知 $x^2>1$，求解集。\n2. 解方程 $x+1=3$。",
                        "images": {},
                    },
                    "prunedResult": {
                        "parsing_res_list": [
                            {
                                "block_bbox": [0, 5, 900, 30],
                                "block_content": "测试卷",
                                "block_label": "text",
                            },
                            {
                                "block_bbox": [80, 80, 300, 120],
                                "block_content": "一、选择题",
                                "block_label": "text",
                            },
                            {
                                "block_bbox": [80, 140, 850, 260],
                                "block_content": "1. 已知 $x^2>1$，求解集。\n2. 解方程 $x+1=3$。",
                                "block_label": "text",
                            },
                        ]
                    },
                },
                {
                    "page_width": 1000,
                    "page_height": 2000,
                    "markdown": {"text": "3. 计算 $2^3$。"},
                    "prunedResult": {
                        "parsing_res_list": [
                            {
                                "block_bbox": [0, 5, 900, 30],
                                "block_content": "测试卷",
                                "block_label": "text",
                            },
                            {
                                "block_bbox": [80, 80, 850, 140],
                                "block_content": "3. 计算 $2^3$。",
                                "block_label": "text",
                            },
                        ]
                    },
                },
            ]
        }
    }
    raw_jsonl = json.dumps(page_payload, ensure_ascii=False)
    parsed = parse_paddleocr_jsonl(raw_jsonl, job_id="job-test")
    assert_equal(len(parsed["pages"]), 2, "paddleocr page count")
    assert_equal(parsed["pages"][0]["blocks"][1]["text"], "一、选择题", "paddleocr block text")

    tree = build_question_tree(parsed["pages"])
    minor_nodes = [node for node in tree["nodes"] if node["level"] == "minor"]
    assert_equal(len(minor_nodes), 3, "ocr recovered minor question count")
    assert minor_nodes[1]["warnings"], "embedded second question should carry recovery warning"
    assert minor_nodes[0]["bbox_pages"], "ocr question should have bbox pages"


def test_math_latex_sanitizer_repairs_array_glitches():
    broken = (
        r"3. 解方程组：\(\left\{\ begin{array}{l}x+y=3 \\ x-y=1\end{array}\right.\)"
        "\n"
        r"6. 解方程组：\begin{cases}a+b=2\\a-b=0\end{cases}$"
    )
    fixed = sanitize_math_markdown(broken)
    assert r"\left\{\begin{array}{l}" in fixed
    assert r"\{\ begin{array}" not in fixed
    assert r"：$\begin{cases}" in fixed


def test_question_bank_tags_and_similar_outputs():
    base = PROJECT_ROOT / "outputs" / "tmp_question_bank_test"
    if base.exists():
        shutil.rmtree(base)
    result_root = base / "result"
    bank_dir = result_root / "question_bank"
    bank_dir.mkdir(parents=True)
    taxonomy_map = {
        "kp-linear": {"id": "kp-linear", "name": "一次方程", "dimension": "knowledge_point"},
        "qt-system": {"id": "qt-system", "name": "方程组", "dimension": "question_type"},
        "idea-elim": {"id": "idea-elim", "name": "消元法", "dimension": "solution_idea"},
    }
    question = enrich_question(
        {
            "bank_id": "sample",
            "item_id": "q1",
            "grade_band": "junior",
            "difficulty": "medium",
            "question_type_field": "解方程组",
            "stem": "解方程组 x+y=3, x-y=1",
            "answer": "x=2,y=1",
            "tags": ["kp-linear", "qt-system", "idea-elim"],
        },
        taxonomy_map,
    )
    (bank_dir / "questions.jsonl").write_text(
        json.dumps(question, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    results = similar_questions("解方程组，用消元法求一次方程组", mode="knowledge", limit=3, result_root=result_root)
    assert_equal(len(results), 1, "question bank similar result count")
    assert "一次方程" in results[0]["tags"]
    markdown = render_similar_markdown({"knowledge": results})
    assert "考点类似" in markdown
    assert "方程组" in markdown


def main():
    test_question_counting()
    test_reader_normalizes_split_heading_question()
    test_reader_merges_choices_and_drops_solution_fragments()
    test_answer_pair_counting()
    test_qc_major_issue_normalization()
    test_q6_regression_samples()
    test_q11_regression_samples()
    test_final_outputs_basic_health()
    test_math_audit_blocks_unsafe_rational_answer()
    test_math_audit_accepts_guarded_rational_answer()
    test_math_audit_blocks_wrong_outside_between_shape()
    test_math_audit_accepts_correct_outside_between_shape()
    test_originality_report_flags_near_copy()
    test_qc_blocks_incomplete_question_stem()
    test_qc_blocks_wrong_recurrence_closed_form()
    test_result_store_save_and_restore()
    test_result_store_can_disable_global_fallback_for_itemwise_runs()
    test_result_store_saves_qc_verified_items_only()
    test_result_store_groups_exact_decomposer_and_restores_weighted_items()
    test_itemwise_outputs_combine_with_global_numbering()
    test_itemwise_combine_rejects_excessive_exact_duplicates()
    test_selected_item_id_parser()
    test_pipeline_pause_flag_waits_until_resume()
    test_professional_selector_prefers_passed_high_score_candidate()
    test_professional_selector_penalizes_duplicate_questions()
    test_time_machine_scans_replay_nodes()
    test_paddleocr_jsonl_parsing_and_question_tree()
    test_math_latex_sanitizer_repairs_array_glitches()
    test_question_bank_tags_and_similar_outputs()
    print(json.dumps({"status": "ok", "cases": len(CASES)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
