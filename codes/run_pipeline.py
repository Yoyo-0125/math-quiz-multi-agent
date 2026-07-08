import argparse
import json
import os
import shutil
from pathlib import Path

from budget import BudgetExceededError
from common import get_token_usage_summary, read_json_file, set_token_budget
from decomposer_agent import revise as revise_decomposer
from decomposer_agent import run as run_decomposer
from generator_agent import revise as revise_generator
from generator_agent import run as run_generator
from qc_agent import run as run_qc
from render_vscode_preview import render_preview
from reviewer_agent import run as run_reviewer
from validators import ValidationError


DEFAULT_REVIEW_SCORE_THRESHOLD = 90
DEFAULT_MAX_REVIEW_REVISION_ROUNDS = 2

DEFAULT_QC_SCORE_THRESHOLD = 90
DEFAULT_MAX_QC_REVISION_ROUNDS = 2


class PipelineQualityError(RuntimeError):
    pass


DEFAULT_OPTIONS = {
    "input": "examples/input.md",
    "output_dir": "outputs",
    "review_threshold": DEFAULT_REVIEW_SCORE_THRESHOLD,
    "max_review_rounds": DEFAULT_MAX_REVIEW_REVISION_ROUNDS,
    "qc_threshold": DEFAULT_QC_SCORE_THRESHOLD,
    "max_qc_rounds": DEFAULT_MAX_QC_REVISION_ROUNDS,
    "max_generated_questions": 1,
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
        "decomposer": "deepseek-v4-flash",
        "reviewer": "deepseek-v4-flash",
        "generator": "deepseek-v4-flash",
        "qc": "deepseek-v4-flash",
    },
    "thinking": {
        "decomposer": False,
        "reviewer": False,
        "generator": True,
        "qc": True,
    },
    "reasoning_effort": {
        "decomposer": "medium",
        "reviewer": "medium",
        "generator": "medium",
        "qc": "medium",
    },
    "token_budget": {
        "total_warning": 180000,
        "total_stop": 270000,
        "single_warning": 45000,
        "single_stop": 65000,
    },
}


def load_options(config_path):
    options = {
        **DEFAULT_OPTIONS,
        "models": dict(DEFAULT_OPTIONS["models"]),
        "thinking": dict(DEFAULT_OPTIONS["thinking"]),
        "reasoning_effort": dict(DEFAULT_OPTIONS["reasoning_effort"]),
        "token_budget": dict(DEFAULT_OPTIONS["token_budget"]),
        "generation_profile": dict(DEFAULT_OPTIONS["generation_profile"]),
    }

    path = Path(config_path)
    if path.exists():
        with path.open("r", encoding="utf-8") as file:
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

    return options


def apply_model_options(options):
    models = options.get("models", {})
    thinking = options.get("thinking", {})
    reasoning_effort = options.get("reasoning_effort", {})
    env_mapping = {
        "default": "DEEPSEEK_MODEL",
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


def print_effective_options(options):
    print("=== Effective options ===")
    print(f"input: {options['input']}")
    print(f"output_dir: {options['output_dir']}")
    print(f"review_threshold: {options['review_threshold']}")
    print(f"max_review_rounds: {options['max_review_rounds']}")
    print(f"qc_threshold: {options['qc_threshold']}")
    print(f"max_qc_rounds: {options['max_qc_rounds']}")
    print(f"max_generated_questions: {options['max_generated_questions']}")
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
    print()


def output_path(output_dir, filename):
    return str(Path(output_dir) / filename)


def clear_stale_final_outputs(output_dir):
    final_filenames = [
        "decomposer_final.json",
        "reviewer_final.json",
        "generator_final.json",
        "generated_questions_final.md",
        "answer_key_final.md",
        "qc_final.json",
        "vscode_preview.md",
    ]
    for filename in final_filenames:
        path = Path(output_dir) / filename
        if path.exists():
            path.unlink()


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

    print("=== Round 0: running Decomposer ===")
    run_decomposer(input_path, current_decomposer_output)

    final_decomposer_output = current_decomposer_output
    final_reviewer_output = current_reviewer_output
    reviewer_passed = False

    for round_index in range(max_review_revision_rounds + 1):
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
):
    current_generator_output = output_path(output_dir, "generator_round0.json")
    current_questions_output = output_path(output_dir, "generated_questions_round0.md")
    current_answer_key_output = output_path(output_dir, "answer_key_round0.md")
    current_qc_output = output_path(output_dir, "qc_round0.json")

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

    for round_index in range(max_qc_revision_rounds + 1):
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
        raise PipelineQualityError(
            "QC 未达到通过标准，已停止。输入过多或题目难度过大，请拆分后再输入。"
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


def run_pipeline(
    input_path,
    output_dir,
    review_score_threshold=DEFAULT_REVIEW_SCORE_THRESHOLD,
    max_review_revision_rounds=DEFAULT_MAX_REVIEW_REVISION_ROUNDS,
    qc_score_threshold=DEFAULT_QC_SCORE_THRESHOLD,
    max_qc_revision_rounds=DEFAULT_MAX_QC_REVISION_ROUNDS,
    max_generated_questions=1,
    generation_profile=None,
):
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    clear_stale_final_outputs(output_dir)

    decomposer_final_path, reviewer_final_path = run_decomposer_reviewer_loop(
        input_path=input_path,
        output_dir=output_dir,
        review_score_threshold=review_score_threshold,
        max_review_revision_rounds=max_review_revision_rounds,
    )

    if generation_profile and generation_profile.get("question_count_mode") == "match_source":
        decomposer_result = read_json_file(decomposer_final_path)
        source_count = (
            decomposer_result.get("source_structure", {}).get("total_question_count")
        )
        if isinstance(source_count, int) and source_count > 0:
            generation_profile = dict(generation_profile)
            generation_profile["question_count"] = source_count
            max_generated_questions = source_count

    return run_generator_qc_loop(
        input_path=input_path,
        decomposer_final_path=decomposer_final_path,
        reviewer_final_path=reviewer_final_path,
        output_dir=output_dir,
        qc_score_threshold=qc_score_threshold,
        max_qc_revision_rounds=max_qc_revision_rounds,
        max_generated_questions=max_generated_questions,
        generation_profile=generation_profile,
    ) + (decomposer_final_path, reviewer_final_path)


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
    parser.add_argument("--total-token-warning", type=int, default=None)
    parser.add_argument("--total-token-stop", type=int, default=None)
    parser.add_argument("--single-token-warning", type=int, default=None)
    parser.add_argument("--single-token-stop", type=int, default=None)
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
        )
    except (BudgetExceededError, PipelineQualityError, ValidationError) as error:
        print(f"\n{error}")
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


if __name__ == "__main__":
    main()
