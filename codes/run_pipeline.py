import argparse
import json
import os
import re
import shutil
import time
from pathlib import Path

from budget import BudgetExceededError
from common import get_token_usage_summary, read_json_file, set_token_budget
from decomposer_agent import revise as revise_decomposer
from decomposer_agent import run as run_decomposer
from generator_agent import revise as revise_generator
from generator_agent import run as run_generator
from math_latex_sanitizer import sanitize_math_markdown
from qc_agent import run as run_qc
from reader_agent import run as run_reader
from render_vscode_preview import render_preview
from result_store import (
    restore_recent_result,
    save_success_record,
    save_verified_items,
    split_numbered_blocks,
)
from reviewer_agent import run as run_reviewer
from validators import ValidationError


DEFAULT_REVIEW_SCORE_THRESHOLD = 80
DEFAULT_MAX_REVIEW_REVISION_ROUNDS = 2

DEFAULT_QC_SCORE_THRESHOLD = 90
DEFAULT_MAX_QC_REVISION_ROUNDS = 4
PAUSE_FILENAME = "pipeline_pause.flag"


class PipelineQualityError(RuntimeError):
    pass


def pause_flag_path(output_dir):
    return Path(output_dir) / PAUSE_FILENAME


def wait_if_paused(output_dir, label="pipeline"):
    path = pause_flag_path(output_dir)
    announced = False
    while path.exists():
        if not announced:
            print(f"Pipeline paused before {label}. Remove {PAUSE_FILENAME} or click Resume to continue.", flush=True)
            announced = True
        time.sleep(1)
    if announced:
        print(f"Pipeline resumed: {label}", flush=True)


def is_timeout_error(error):
    text = str(error).lower()
    return "timed out" in text or "timeout" in text or "超时" in text


DEFAULT_OPTIONS = {
    "input": "examples/input.md",
    "output_dir": "outputs",
    "review_threshold": DEFAULT_REVIEW_SCORE_THRESHOLD,
    "max_review_rounds": DEFAULT_MAX_REVIEW_REVISION_ROUNDS,
    "qc_threshold": DEFAULT_QC_SCORE_THRESHOLD,
    "max_qc_rounds": DEFAULT_MAX_QC_REVISION_ROUNDS,
    "max_generated_questions": 1,
    "selected_item_ids": "",
    "generation_profile": {
        "question_count": 1,
        "question_count_mode": "match_source",
        "difficulty": "match_input",
        "knowledge_points": "",
        "question_types": "",
        "style": "match_input",
        "answer_detail": "concise",
        "allow_input_compaction": False,
    },
    "models": {
        "default": "deepseek-v4-flash",
        "reader": "deepseek-v4-pro",
        "decomposer": "deepseek-v4-flash",
        "reviewer": "deepseek-v4-flash",
        "generator": "deepseek-v4-pro",
        "qc": "deepseek-v4-pro",
    },
    "thinking": {
        "reader": False,
        "decomposer": False,
        "reviewer": False,
        "generator": True,
        "qc": True,
    },
    "reasoning_effort": {
        "reader": "medium",
        "decomposer": "medium",
        "reviewer": "medium",
        "generator": "high",
        "qc": "high",
    },
    "token_budget": {
        "total_warning": 320000,
        "total_stop": 480000,
        "single_warning": 100000,
        "single_stop": 150000,
    },
    "runtime": {
        "request_timeout_seconds": 90,
        "generator_warning_seconds": 60,
        "generator_timeout_seconds": 120,
        "fallback_on_error": True,
        "fallback_on_timeout": True,
    },
    "result_store": {
        "enabled": True,
        "dir": "result",
    },
}


def load_options(config_path):
    options = {
        **DEFAULT_OPTIONS,
        "models": dict(DEFAULT_OPTIONS["models"]),
        "thinking": dict(DEFAULT_OPTIONS["thinking"]),
        "reasoning_effort": dict(DEFAULT_OPTIONS["reasoning_effort"]),
        "token_budget": dict(DEFAULT_OPTIONS["token_budget"]),
        "runtime": dict(DEFAULT_OPTIONS["runtime"]),
        "result_store": dict(DEFAULT_OPTIONS["result_store"]),
        "generation_profile": dict(DEFAULT_OPTIONS["generation_profile"]),
    }

    path = Path(config_path)
    if path.exists():
        with path.open("r", encoding="utf-8-sig") as file:
            config_options = json.load(file)

        for key, value in config_options.items():
            if key == "models" and isinstance(value, dict):
                options["models"].update(value)
            elif key == "thinking" and isinstance(value, dict):
                options["thinking"].update(value)
            elif key == "reasoning_effort" and isinstance(value, dict):
                options["reasoning_effort"].update(value)
            elif key == "token_budget" and isinstance(value, dict):
                options["token_budget"].update(value)
            elif key == "runtime" and isinstance(value, dict):
                options["runtime"].update(value)
            elif key == "result_store" and isinstance(value, dict):
                options["result_store"].update(value)
            elif key == "generation_profile" and isinstance(value, dict):
                options["generation_profile"].update(value)
            else:
                options[key] = value

    options["max_generated_questions"] = int(
        options["generation_profile"].get(
            "question_count",
            options.get("max_generated_questions", 1),
        )
    )

    return options


def apply_cli_overrides(options, args):
    cli_values = {
        "input": args.input,
        "output_dir": args.output_dir,
        "review_threshold": args.review_threshold,
        "max_review_rounds": args.max_review_rounds,
        "qc_threshold": args.qc_threshold,
        "max_qc_rounds": args.max_qc_rounds,
        "max_generated_questions": args.max_generated_questions,
        "selected_item_ids": args.selected_item_ids,
    }

    for key, value in cli_values.items():
        if value is not None:
            options[key] = value

    generation_profile = options.get("generation_profile", {})
    generation_cli_values = {
        "question_count": args.question_count,
        "question_count_mode": args.question_count_mode,
        "difficulty": args.difficulty,
        "knowledge_points": args.knowledge_points,
        "question_types": args.question_types,
        "style": args.style,
        "answer_detail": args.answer_detail,
    }
    for key, value in generation_cli_values.items():
        if value is not None:
            generation_profile[key] = value
    if args.question_count is not None and args.question_count_mode is None:
        generation_profile["question_count_mode"] = "fixed"
    if args.no_input_compaction:
        generation_profile["allow_input_compaction"] = False
    if args.max_generated_questions is not None and args.question_count is None:
        generation_profile["question_count"] = args.max_generated_questions
    options["generation_profile"] = generation_profile
    options["max_generated_questions"] = int(generation_profile.get("question_count", 1))

    token_budget = options.get("token_budget", {})
    token_cli_values = {
        "total_warning": args.total_token_warning,
        "total_stop": args.total_token_stop,
        "single_warning": args.single_token_warning,
        "single_stop": args.single_token_stop,
    }
    for key, value in token_cli_values.items():
        if value is not None:
            token_budget[key] = value
    options["token_budget"] = token_budget

    runtime = options.get("runtime", {})
    if args.request_timeout is not None:
        runtime["request_timeout_seconds"] = args.request_timeout
    if args.generator_warning is not None:
        runtime["generator_warning_seconds"] = args.generator_warning
    if args.generator_timeout is not None:
        runtime["generator_timeout_seconds"] = args.generator_timeout
    options["runtime"] = runtime

    return options


def apply_model_options(options):
    models = options.get("models", {})
    thinking = options.get("thinking", {})
    reasoning_effort = options.get("reasoning_effort", {})
    env_mapping = {
        "default": "DEEPSEEK_MODEL",
        "reader": "DEEPSEEK_READER_MODEL",
        "decomposer": "DEEPSEEK_DECOMPOSER_MODEL",
        "reviewer": "DEEPSEEK_REVIEWER_MODEL",
        "generator": "DEEPSEEK_GENERATOR_MODEL",
        "qc": "DEEPSEEK_QC_MODEL",
    }

    for model_key, env_key in env_mapping.items():
        model_name = models.get(model_key)
        if model_name:
            os.environ[env_key] = str(model_name)

    for agent_name, enabled in thinking.items():
        os.environ[f"DEEPSEEK_{agent_name.upper()}_THINKING"] = (
            "enabled" if enabled else "disabled"
        )

    for agent_name, effort in reasoning_effort.items():
        if effort:
            os.environ[f"DEEPSEEK_{agent_name.upper()}_REASONING_EFFORT"] = str(effort)

    runtime = options.get("runtime", {})
    if runtime.get("request_timeout_seconds"):
        os.environ["DEEPSEEK_REQUEST_TIMEOUT"] = str(runtime["request_timeout_seconds"])
    if runtime.get("generator_warning_seconds"):
        os.environ["DEEPSEEK_GENERATOR_REQUEST_WARNING"] = str(
            runtime["generator_warning_seconds"]
        )
    if runtime.get("generator_timeout_seconds"):
        os.environ["DEEPSEEK_GENERATOR_REQUEST_TIMEOUT"] = str(
            runtime["generator_timeout_seconds"]
        )


def print_effective_options(options):
    print("=== Effective options ===")
    print(f"input: {options['input']}")
    print(f"output_dir: {options['output_dir']}")
    print(f"review_threshold: {options['review_threshold']}")
    print(f"max_review_rounds: {options['max_review_rounds']}")
    print(f"qc_threshold: {options['qc_threshold']}")
    print(f"max_qc_rounds: {options['max_qc_rounds']}")
    print(f"max_generated_questions: {options['max_generated_questions']}")
    if options.get("selected_item_ids"):
        print(f"selected_item_ids: {options['selected_item_ids']}")
    print("generation_profile:")
    for key, value in options.get("generation_profile", {}).items():
        print(f"  {key}: {value}")
    if options.get("generation_profile", {}).get("question_count_mode") == "match_source":
        print("  resolved_question_count: after Decomposer source_structure.total_question_count")
    print("models:")
    for key, value in options.get("models", {}).items():
        print(f"  {key}: {value}")
    print("thinking:")
    for key, value in options.get("thinking", {}).items():
        print(f"  {key}: {value}")
    print("reasoning_effort:")
    for key, value in options.get("reasoning_effort", {}).items():
        print(f"  {key}: {value}")
    print("token_budget:")
    for key, value in options.get("token_budget", {}).items():
        print(f"  {key}: {value}")
    print("runtime:")
    for key, value in options.get("runtime", {}).items():
        print(f"  {key}: {value}")
    print("result_store:")
    for key, value in options.get("result_store", {}).items():
        print(f"  {key}: {value}")
    print()


def output_path(output_dir, filename):
    return str(Path(output_dir) / filename)


def read_text(path):
    return Path(path).read_text(encoding="utf-8")


def write_text(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def clear_stale_final_outputs(output_dir):
    final_filenames = [
        "decomposer_final.json",
        "reviewer_final.json",
        "generator_final.json",
        "generated_questions_final.md",
        "answer_key_final.md",
        "qc_final.json",
        "vscode_preview.md",
        "question_bank_similar.md",
        "question_bank_similar.json",
        "reader_result.json",
        "reader_clean_input.md",
        PAUSE_FILENAME,
    ]
    for filename in final_filenames:
        path = Path(output_dir) / filename
        if path.exists():
            path.unlink()
    items_dir = Path(output_dir) / "items"
    if items_dir.exists():
        shutil.rmtree(items_dir)


def write_failure_status(output_dir, error):
    path = Path(output_dir) / "pipeline_failed.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "status": "failed",
                "reason": str(error),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return str(path)


def build_single_item_input(item):
    section = item.get("section") or "default"
    text = item.get("text", "").strip()
    return "\n".join(
        [
            f"# Reader Item {item.get('id', 1)}",
            "",
            f"## {section}",
            "",
            f"1. {text}",
            "",
        ]
    )


def renumber_markdown_block(block_text, number):
    text = (block_text or "").strip()
    if not text:
        return f"{number}. "
    if re.match(r"^\s*\d+[.)]\s+", text):
        return re.sub(r"^\s*\d+[.)]\s+", f"{number}. ", text, count=1)
    return f"{number}. {text}"


def duplicate_guard_key(block_text):
    text = re.sub(r"^\s*\d+[.)]\s*", "", block_text or "")
    text = re.sub(r"\s+", "", text)
    return text[:240]


def ensure_no_excessive_exact_duplicates(question_blocks, max_repeat=2):
    counts = {}
    examples = {}
    for block in question_blocks:
        key = duplicate_guard_key(block)
        if len(key) < 24:
            continue
        counts[key] = counts.get(key, 0) + 1
        examples.setdefault(key, block.splitlines()[0][:160])
    repeated = [
        (key, count)
        for key, count in counts.items()
        if count > max_repeat
    ]
    if not repeated:
        return
    repeated.sort(key=lambda item: item[1], reverse=True)
    key, count = repeated[0]
    raise PipelineQualityError(
        "Combined output contains excessive duplicate questions: "
        f"{count} repeats of {examples.get(key, key)}"
    )


def parse_selected_item_ids(value):
    if value in [None, ""]:
        return []
    if isinstance(value, int):
        return [value] if value > 0 else []
    if isinstance(value, list):
        numbers = []
        for item in value:
            numbers.extend(parse_selected_item_ids(item))
        return sorted(set(numbers))
    numbers = []
    for part in re.split(r"[,，\s]+", str(value).strip()):
        if not part:
            continue
        range_match = re.match(r"^(\d+)\s*[-~至]\s*(\d+)$", part)
        if range_match:
            start, end = int(range_match.group(1)), int(range_match.group(2))
            if start > end:
                start, end = end, start
            numbers.extend(range(start, end + 1))
            continue
        if part.isdigit():
            numbers.append(int(part))
    return sorted({number for number in numbers if number > 0})


def need_decomposer_revision(reviewer_result, review_score_threshold):
    is_valid = reviewer_result.get("is_valid", False)
    score = reviewer_result.get("score", 0)

    if not isinstance(score, int):
        score = 0

    return (not is_valid) or score < review_score_threshold


def need_generator_revision(qc_result, qc_score_threshold):
    is_passed = qc_result.get("is_passed", False)
    score = qc_result.get("score", 0)

    if not isinstance(score, int):
        score = 0

    return (not is_passed) or score < qc_score_threshold


def run_decomposer_reviewer_loop(
    input_path,
    output_dir,
    review_score_threshold=DEFAULT_REVIEW_SCORE_THRESHOLD,
    max_review_revision_rounds=DEFAULT_MAX_REVIEW_REVISION_ROUNDS,
):
    current_decomposer_output = output_path(output_dir, "decomposer_round0.json")
    current_reviewer_output = output_path(output_dir, "reviewer_round0.json")

    wait_if_paused(output_dir, "Decomposer")
    print("=== Round 0: running Decomposer ===")
    run_decomposer(input_path, current_decomposer_output)

    final_decomposer_output = current_decomposer_output
    final_reviewer_output = current_reviewer_output
    reviewer_passed = False

    for round_index in range(max_review_revision_rounds + 1):
        wait_if_paused(output_dir, f"Reviewer round {round_index}")
        print(f"\n=== Round {round_index}: running Reviewer ===")
        run_reviewer(
            input_path,
            current_decomposer_output,
            current_reviewer_output,
        )

        reviewer_result = read_json_file(current_reviewer_output)

        score = reviewer_result.get("score", 0)
        is_valid = reviewer_result.get("is_valid", False)

        print(f"Reviewer result: is_valid={is_valid}, score={score}")

        final_decomposer_output = current_decomposer_output
        final_reviewer_output = current_reviewer_output

        if not need_decomposer_revision(reviewer_result, review_score_threshold):
            print("Reviewer passed; Decomposer revision is not needed.")
            reviewer_passed = True
            break

        if round_index >= max_review_revision_rounds:
            print("Max Decomposer revision rounds reached.")
            break

        next_round = round_index + 1
        next_decomposer_output = output_path(output_dir, f"decomposer_round{next_round}.json")
        next_reviewer_output = output_path(output_dir, f"reviewer_round{next_round}.json")

        print(f"Reviewer failed; revising Decomposer for round {next_round}.")
        wait_if_paused(output_dir, f"Decomposer revision round {next_round}")

        revise_decomposer(
            input_path=input_path,
            previous_decomposer_path=current_decomposer_output,
            reviewer_feedback_path=current_reviewer_output,
            output_path=next_decomposer_output,
        )

        current_decomposer_output = next_decomposer_output
        current_reviewer_output = next_reviewer_output

    if not reviewer_passed:
        raise PipelineQualityError(
            "Reviewer 未达到通过标准，已停止。请降低输入复杂度、提高修订轮数，或拆分原题后再运行。"
        )

    decomposer_final_output = output_path(output_dir, "decomposer_final.json")
    reviewer_final_output = output_path(output_dir, "reviewer_final.json")
    shutil.copyfile(final_decomposer_output, decomposer_final_output)
    shutil.copyfile(final_reviewer_output, reviewer_final_output)

    return decomposer_final_output, reviewer_final_output


def run_generator_qc_loop(
    input_path,
    decomposer_final_path,
    reviewer_final_path,
    output_dir,
    qc_score_threshold=DEFAULT_QC_SCORE_THRESHOLD,
    max_qc_revision_rounds=DEFAULT_MAX_QC_REVISION_ROUNDS,
    max_generated_questions=1,
    generation_profile=None,
    skip_qc=False,
    result_store_options=None,
):
    current_generator_output = output_path(output_dir, "generator_round0.json")
    current_questions_output = output_path(output_dir, "generated_questions_round0.md")
    current_answer_key_output = output_path(output_dir, "answer_key_round0.md")
    current_qc_output = output_path(output_dir, "qc_round0.json")

    wait_if_paused(output_dir, "Generator")
    print("\n=== Round 0: running Generator ===")
    run_generator(
        original_input_path=input_path,
        decomposer_json_path=decomposer_final_path,
        reviewer_json_path=reviewer_final_path,
        output_json_path=current_generator_output,
        questions_output_path=current_questions_output,
        answer_key_output_path=current_answer_key_output,
        max_generated_questions=max_generated_questions,
        generation_profile=generation_profile,
    )

    final_generator_output = current_generator_output
    final_questions_output = current_questions_output
    final_answer_key_output = current_answer_key_output
    final_qc_output = current_qc_output
    qc_passed = False

    if skip_qc:
        generator_final_output = output_path(output_dir, "generator_final.json")
        questions_final_output = output_path(output_dir, "generated_questions_final.md")
        answer_key_final_output = output_path(output_dir, "answer_key_final.md")
        qc_final_output = output_path(output_dir, "qc_final.json")

        shutil.copyfile(final_generator_output, generator_final_output)
        shutil.copyfile(final_questions_output, questions_final_output)
        shutil.copyfile(final_answer_key_output, answer_key_final_output)
        Path(qc_final_output).write_text(
            json.dumps(
                {
                    "is_passed": None,
                    "score": None,
                    "skipped": True,
                    "reason": "QC skipped by --skip-qc; use local self-check scripts for this test run.",
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        return (
            generator_final_output,
            questions_final_output,
            answer_key_final_output,
            qc_final_output,
        )

    for round_index in range(max_qc_revision_rounds + 1):
        wait_if_paused(output_dir, f"QC round {round_index}")
        print(f"\n=== Round {round_index}: running QC ===")
        run_qc(
            original_input_path=input_path,
            decomposer_json_path=decomposer_final_path,
            generator_json_path=current_generator_output,
            questions_path=current_questions_output,
            answer_key_path=current_answer_key_output,
            output_json_path=current_qc_output,
            generation_profile=generation_profile,
        )

        qc_result = read_json_file(current_qc_output)
        result_store_options = result_store_options or {}
        if result_store_options.get("enabled", True):
            decomposer_result_for_store = read_json_file(decomposer_final_path)
            stored_items = save_verified_items(
                decomposer_result=decomposer_result_for_store,
                questions_markdown=Path(current_questions_output).read_text(encoding="utf-8"),
                answer_key_markdown=Path(current_answer_key_output).read_text(encoding="utf-8"),
                qc_result=qc_result,
                result_root=result_store_options.get("dir", "result"),
                source_round=round_index,
            )
            if stored_items:
                print(f"Result library: saved {len(stored_items)} QC-verified item(s).")

        score = qc_result.get("score", 0)
        is_passed = qc_result.get("is_passed", False)

        print(f"QC result: is_passed={is_passed}, score={score}")

        final_generator_output = current_generator_output
        final_questions_output = current_questions_output
        final_answer_key_output = current_answer_key_output
        final_qc_output = current_qc_output

        if not need_generator_revision(qc_result, qc_score_threshold):
            print("QC passed; Generator revision is not needed.")
            qc_passed = True
            break

        if round_index >= max_qc_revision_rounds:
            print("Max Generator revision rounds reached.")
            break

        next_round = round_index + 1
        next_generator_output = output_path(output_dir, f"generator_round{next_round}.json")
        next_questions_output = output_path(output_dir, f"generated_questions_round{next_round}.md")
        next_answer_key_output = output_path(output_dir, f"answer_key_round{next_round}.md")
        next_qc_output = output_path(output_dir, f"qc_round{next_round}.json")

        print(f"QC failed; revising Generator for round {next_round}.")
        wait_if_paused(output_dir, f"Generator revision round {next_round}")

        revise_generator(
            original_input_path=input_path,
            decomposer_json_path=decomposer_final_path,
            reviewer_json_path=reviewer_final_path,
            previous_generator_json_path=current_generator_output,
            previous_questions_path=current_questions_output,
            previous_answer_key_path=current_answer_key_output,
            qc_feedback_path=current_qc_output,
            output_json_path=next_generator_output,
            questions_output_path=next_questions_output,
            answer_key_output_path=next_answer_key_output,
            max_generated_questions=max_generated_questions,
            generation_profile=generation_profile,
        )

        current_generator_output = next_generator_output
        current_questions_output = next_questions_output
        current_answer_key_output = next_answer_key_output
        current_qc_output = next_qc_output

    if not qc_passed:
        print(
            "QC 未达到通过标准；将保留最新 Generator 输出，供人工检查或继续调参。"
        )

    generator_final_output = output_path(output_dir, "generator_final.json")
    questions_final_output = output_path(output_dir, "generated_questions_final.md")
    answer_key_final_output = output_path(output_dir, "answer_key_final.md")
    qc_final_output = output_path(output_dir, "qc_final.json")

    shutil.copyfile(final_generator_output, generator_final_output)
    shutil.copyfile(final_questions_output, questions_final_output)
    shutil.copyfile(final_answer_key_output, answer_key_final_output)
    shutil.copyfile(final_qc_output, qc_final_output)

    return (
        generator_final_output,
        questions_final_output,
        answer_key_final_output,
        qc_final_output,
    )


def make_single_item_profile(generation_profile):
    profile = dict(generation_profile or {})
    profile["question_count"] = 1
    profile["max_generated_questions"] = 1
    profile["question_count_mode"] = "fixed"
    profile["allow_input_compaction"] = False
    return profile


def run_single_reader_item(
    item,
    item_output_dir,
    review_score_threshold,
    max_review_revision_rounds,
    qc_score_threshold,
    max_qc_revision_rounds,
    generation_profile,
    skip_qc,
    result_store_options,
):
    item_output_dir = Path(item_output_dir)
    item_output_dir.mkdir(parents=True, exist_ok=True)
    item_input_path = item_output_dir / "input.md"
    write_text(item_input_path, build_single_item_input(item))

    single_profile = make_single_item_profile(generation_profile)
    try:
        decomposer_final_path, reviewer_final_path = run_decomposer_reviewer_loop(
            input_path=str(item_input_path),
            output_dir=str(item_output_dir),
            review_score_threshold=review_score_threshold,
            max_review_revision_rounds=max_review_revision_rounds,
        )
        (
            generator_final_path,
            questions_final_path,
            answer_key_final_path,
            qc_final_path,
        ) = run_generator_qc_loop(
            input_path=str(item_input_path),
            decomposer_final_path=decomposer_final_path,
            reviewer_final_path=reviewer_final_path,
            output_dir=str(item_output_dir),
            qc_score_threshold=qc_score_threshold,
            max_qc_revision_rounds=max_qc_revision_rounds,
            max_generated_questions=1,
            generation_profile=single_profile,
            skip_qc=skip_qc,
            result_store_options=result_store_options,
        )
        return {
            "item_id": item.get("id"),
            "status": "ok",
            "input": str(item_input_path),
            "decomposer": decomposer_final_path,
            "reviewer": reviewer_final_path,
            "generator": generator_final_path,
            "questions": questions_final_path,
            "answer_key": answer_key_final_path,
            "qc": qc_final_path,
        }
    except Exception as error:
        result_store_options = result_store_options or {}
        if result_store_options.get("enabled", True):
            status = restore_recent_result(
                item_output_dir,
                result_root=result_store_options.get("dir", "result"),
                allow_global_record=False,
            )
            if status:
                return {
                    "item_id": item.get("id"),
                    "status": "fallback",
                    "error": str(error),
                    "input": str(item_input_path),
                    "questions": str(item_output_dir / "generated_questions_final.md"),
                    "answer_key": str(item_output_dir / "answer_key_final.md"),
                    "qc": str(item_output_dir / "qc_final.json"),
                    "fallback": status,
                }
        raise


def first_numbered_block(markdown_text):
    blocks = split_numbered_blocks(markdown_text)
    if blocks:
        return blocks[sorted(blocks.keys())[0]]
    return markdown_text.strip()


def combine_itemwise_outputs(output_dir, item_results):
    question_blocks = []
    answer_blocks = []
    qc_results = []
    generator_results = []
    decomposer_results = []
    reviewer_results = []

    for index, item_result in enumerate(item_results, start=1):
        question_text = first_numbered_block(read_text(item_result["questions"]))
        answer_text = first_numbered_block(read_text(item_result["answer_key"]))
        question_blocks.append(renumber_markdown_block(question_text, index))
        answer_blocks.append(renumber_markdown_block(answer_text, index))

        if item_result.get("qc") and Path(item_result["qc"]).exists():
            qc_results.append(read_json_file(item_result["qc"]))
        if item_result.get("generator") and Path(item_result["generator"]).exists():
            generator_results.append(read_json_file(item_result["generator"]))
        if item_result.get("decomposer") and Path(item_result["decomposer"]).exists():
            decomposer_results.append(read_json_file(item_result["decomposer"]))
        if item_result.get("reviewer") and Path(item_result["reviewer"]).exists():
            reviewer_results.append(read_json_file(item_result["reviewer"]))

    questions_final_output = output_path(output_dir, "generated_questions_final.md")
    answer_key_final_output = output_path(output_dir, "answer_key_final.md")
    generator_final_output = output_path(output_dir, "generator_final.json")
    qc_final_output = output_path(output_dir, "qc_final.json")
    decomposer_final_output = output_path(output_dir, "decomposer_final.json")
    reviewer_final_output = output_path(output_dir, "reviewer_final.json")

    ensure_no_excessive_exact_duplicates(question_blocks)

    questions_markdown = sanitize_math_markdown("\n\n".join(question_blocks))
    answer_key_markdown = sanitize_math_markdown("\n\n".join(answer_blocks))
    write_text(questions_final_output, questions_markdown + "\n")
    write_text(answer_key_final_output, answer_key_markdown + "\n")

    generator_final = {
        "questions_markdown": questions_markdown,
        "answer_key_markdown": answer_key_markdown,
        "generation_notes": ["Combined from per-reader-item pipeline runs."],
        "possible_risks": [],
        "item_results": item_results,
        "item_generator_results": generator_results,
    }
    write_text(generator_final_output, json.dumps(generator_final, ensure_ascii=False, indent=2))

    qc_scores = [
        item.get("score")
        for item in qc_results
        if isinstance(item.get("score"), int)
    ]
    qc_final = {
        "is_passed": all(item.get("is_passed") is True for item in qc_results) if qc_results else None,
        "score": int(sum(qc_scores) / len(qc_scores)) if qc_scores else None,
        "question_issues": [],
        "answer_issues": [],
        "style_issues": [],
        "revision_instructions": [],
        "qc_summary": "Combined from per-reader-item QC results.",
        "item_qc_results": qc_results,
    }
    write_text(qc_final_output, json.dumps(qc_final, ensure_ascii=False, indent=2))

    decomposer_final = {
        "topic": "itemwise_reader_pipeline",
        "subtopics": [],
        "grade_level": "mixed",
        "question_types": ["itemwise"],
        "format_structure": "Reader split input into one item per independent pipeline run.",
        "difficulty_distribution": {"easy": 0, "medium": len(item_results), "hard": 0},
        "style_features": ["per-item generation"],
        "latex_usage": "mixed",
        "generation_constraints": ["combined final output preserves Reader item order"],
        "uncertain_points": [],
        "detected_question_count": len(item_results),
        "source_structure": {
            "total_question_count": len(item_results),
            "items": [
                {
                    "id": index,
                    "kind": "reader_item",
                    "section": "combined",
                    "text": question_blocks[index - 1],
                }
                for index in range(1, len(item_results) + 1)
            ],
        },
        "item_decomposer_results": decomposer_results,
    }
    write_text(decomposer_final_output, json.dumps(decomposer_final, ensure_ascii=False, indent=2))

    reviewer_final = {
        "is_valid": all(item.get("is_valid") is True for item in reviewer_results) if reviewer_results else None,
        "score": None,
        "major_issues": [],
        "minor_issues": [],
        "missing_points": [],
        "review_summary": "Combined from per-reader-item reviewer results.",
        "item_reviewer_results": reviewer_results,
    }
    write_text(reviewer_final_output, json.dumps(reviewer_final, ensure_ascii=False, indent=2))

    return (
        generator_final_output,
        questions_final_output,
        answer_key_final_output,
        qc_final_output,
        decomposer_final_output,
        reviewer_final_output,
    )


def run_itemwise_reader_pipeline(
    reader_result,
    output_dir,
    review_score_threshold,
    max_review_revision_rounds,
    qc_score_threshold,
    max_qc_revision_rounds,
    generation_profile,
    skip_qc,
    result_store_options,
    selected_item_ids=None,
):
    items = reader_result.get("items") or []
    selected_ids = parse_selected_item_ids(selected_item_ids)
    if selected_ids:
        selected_set = set(selected_ids)
        original_items = list(items)
        items = [item for item in original_items if int(item.get("id", 0) or 0) in selected_set]
        if not items and len(original_items) == len(selected_ids):
            # The UI may already have replaced Source with only selected OCR text.
            # In that case Reader ids restart at 1, so running all current items is correct.
            items = original_items
        print(f"Selected Reader items: {', '.join(str(item) for item in selected_ids)}")
    if not items:
        raise PipelineQualityError("Reader 没有拆出可运行的题目，或所选题号不存在。")

    item_results = []
    items_dir = Path(output_dir) / "items"
    for index, item in enumerate(items, start=1):
        wait_if_paused(output_dir, f"Reader item {index}")
        print(f"\n=== Reader item {index}/{len(items)}: running full pipeline ===")
        item_output_dir = items_dir / f"item_{index:03d}"
        item_results.append(
            run_single_reader_item(
                item=item,
                item_output_dir=item_output_dir,
                review_score_threshold=review_score_threshold,
                max_review_revision_rounds=max_review_revision_rounds,
                qc_score_threshold=qc_score_threshold,
                max_qc_revision_rounds=max_qc_revision_rounds,
                generation_profile=generation_profile,
                skip_qc=skip_qc,
                result_store_options=result_store_options,
            )
        )
        combine_itemwise_outputs(output_dir, item_results)
        print(f"Reader item {index}/{len(items)} output: combined final files updated.")

    return combine_itemwise_outputs(output_dir, item_results)


def run_pipeline(
    input_path,
    output_dir,
    review_score_threshold=DEFAULT_REVIEW_SCORE_THRESHOLD,
    max_review_revision_rounds=DEFAULT_MAX_REVIEW_REVISION_ROUNDS,
    qc_score_threshold=DEFAULT_QC_SCORE_THRESHOLD,
    max_qc_revision_rounds=DEFAULT_MAX_QC_REVISION_ROUNDS,
    max_generated_questions=1,
    generation_profile=None,
    skip_qc=False,
    result_store_options=None,
    selected_item_ids=None,
):
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    clear_stale_final_outputs(output_dir)

    reader_output = output_path(output_dir, "reader_result.json")
    reader_clean_input = output_path(output_dir, "reader_clean_input.md")

    wait_if_paused(output_dir, "Reader")
    print("=== Round 0: running Reader ===")
    reader_result = run_reader(input_path, reader_output, reader_clean_input)

    return run_itemwise_reader_pipeline(
        reader_result=reader_result,
        output_dir=output_dir,
        review_score_threshold=review_score_threshold,
        max_review_revision_rounds=max_review_revision_rounds,
        qc_score_threshold=qc_score_threshold,
        max_qc_revision_rounds=max_qc_revision_rounds,
        generation_profile=generation_profile,
        skip_qc=skip_qc,
        result_store_options=result_store_options,
        selected_item_ids=selected_item_ids,
    )


def parse_args():
    parser = argparse.ArgumentParser(description="Run the math quiz multi-agent pipeline.")
    parser.add_argument(
        "--config",
        default="config/pipeline_options.json",
        help="User options JSON file.",
    )
    parser.add_argument("--input", default=None, help="Input markdown file.")
    parser.add_argument("--output-dir", default=None, help="Directory for round and final outputs.")
    parser.add_argument(
        "--review-threshold",
        type=int,
        default=None,
        help="Reviewer score required to stop Decomposer revisions.",
    )
    parser.add_argument(
        "--max-review-rounds",
        type=int,
        default=None,
        help="Maximum Decomposer revision rounds after round 0.",
    )
    parser.add_argument(
        "--qc-threshold",
        type=int,
        default=None,
        help="QC score required to stop Generator revisions.",
    )
    parser.add_argument(
        "--max-qc-rounds",
        type=int,
        default=None,
        help="Maximum Generator revision rounds after round 0.",
    )
    parser.add_argument(
        "--max-generated-questions",
        type=int,
        default=None,
        help="Maximum number of generated questions in test mode.",
    )
    parser.add_argument("--question-count", type=int, default=None)
    parser.add_argument(
        "--question-count-mode",
        choices=["fixed", "match_source"],
        default=None,
    )
    parser.add_argument("--difficulty", default=None)
    parser.add_argument("--knowledge-points", default=None)
    parser.add_argument("--question-types", default=None)
    parser.add_argument("--style", default=None)
    parser.add_argument("--answer-detail", default=None)
    parser.add_argument("--no-input-compaction", action="store_true")
    parser.add_argument(
        "--skip-qc",
        action="store_true",
        help="Run Decomposer, Reviewer, and Generator, then skip QC for local self-check tests.",
    )
    parser.add_argument(
        "--selected-item-ids",
        default=None,
        help="Comma/range list of Reader item ids to run, for example 1,3,5-7.",
    )
    parser.add_argument("--total-token-warning", type=int, default=None)
    parser.add_argument("--total-token-stop", type=int, default=None)
    parser.add_argument("--single-token-warning", type=int, default=None)
    parser.add_argument("--single-token-stop", type=int, default=None)
    parser.add_argument(
        "--request-timeout",
        type=int,
        default=None,
        help="DeepSeek request timeout in seconds.",
    )
    parser.add_argument(
        "--generator-warning",
        type=int,
        default=None,
        help="Generator request warning time in seconds.",
    )
    parser.add_argument(
        "--generator-timeout",
        type=int,
        default=None,
        help="Generator request timeout in seconds.",
    )
    parser.add_argument(
        "--show-options",
        action="store_true",
        help="Print effective options and exit without running the pipeline.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    options = apply_cli_overrides(load_options(args.config), args)
    apply_model_options(options)
    set_token_budget(options.get("token_budget"))
    print_effective_options(options)

    if args.show_options:
        return

    try:
        (
            generator_final_path,
            questions_final_path,
            answer_key_final_path,
            qc_final_path,
            decomposer_final_path,
            reviewer_final_path,
        ) = run_pipeline(
            input_path=options["input"],
            output_dir=options["output_dir"],
            review_score_threshold=options["review_threshold"],
            max_review_revision_rounds=options["max_review_rounds"],
            qc_score_threshold=options["qc_threshold"],
            max_qc_revision_rounds=options["max_qc_rounds"],
            max_generated_questions=options["max_generated_questions"],
            generation_profile=options.get("generation_profile"),
            skip_qc=args.skip_qc,
            result_store_options=options.get("result_store"),
            selected_item_ids=options.get("selected_item_ids"),
        )
    except Exception as error:
        print(f"\n{error}")
        runtime = options.get("runtime", {})
        timeout_error = is_timeout_error(error)
        fallback_enabled = (
            runtime.get("fallback_on_timeout", True)
            if timeout_error
            else runtime.get("fallback_on_error", True)
        )
        result_store = options.get("result_store", {})
        if fallback_enabled and result_store.get("enabled", True):
            status = restore_recent_result(
                options["output_dir"],
                result_root=result_store.get("dir", "result"),
            )
            if status:
                print(
                    "Fallback restored cached result: "
                    f"{status['restored_from']}"
                )
                print(f"Fallback status: {Path(options['output_dir']) / 'result_fallback.json'}")
                return

        failure_path = write_failure_status(options["output_dir"], error)
        print(f"Failure status: {failure_path}")
        summary = get_token_usage_summary()
        if summary:
            print(f"Token usage before stop: {summary['used_tokens']}")
        return

    print("\n=== Pipeline complete ===")
    print(f"Final Decomposer result: {decomposer_final_path}")
    print(f"Final Reviewer result: {reviewer_final_path}")
    print(f"Final Generator result: {generator_final_path}")
    print(f"Final questions: {questions_final_path}")
    print(f"Final answer key: {answer_key_final_path}")
    print(f"Final QC result: {qc_final_path}")

    preview_path = render_preview(options["output_dir"])
    print(f"VSCode preview: {preview_path}")

    summary = get_token_usage_summary()
    if summary:
        print(f"Token usage: {summary['used_tokens']} / {summary['total_stop']}")

    result_store = options.get("result_store", {})
    if result_store.get("enabled", True):
        record_dir = save_success_record(
            options["output_dir"],
            result_root=result_store.get("dir", "result"),
            options=options,
        )
        if record_dir:
            print(f"Saved result record: {record_dir}")


if __name__ == "__main__":
    main()
