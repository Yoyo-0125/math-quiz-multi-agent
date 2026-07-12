import argparse
import json
import shutil
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CODES_DIR = PROJECT_ROOT / "codes"
if str(CODES_DIR) not in sys.path:
    sys.path.insert(0, str(CODES_DIR))

from budget import BudgetExceededError
from common import get_token_usage_summary, set_token_budget
from reader_agent import run as run_reader
from render_vscode_preview import render_preview
from result_store import restore_recent_result
from run_pipeline import (
    DEFAULT_OPTIONS,
    PipelineQualityError,
    apply_model_options,
    clear_stale_final_outputs,
    combine_itemwise_outputs,
    load_options,
    output_path,
    parse_selected_item_ids,
    print_effective_options,
    run_single_reader_item,
    wait_if_paused,
    write_failure_status,
)
from validators import ValidationError


PROFESSIONAL_DEFAULTS = {
    "candidate_count": 2,
    "max_candidate_attempts": 4,
    "selector_min_score": 95,
    "duplicate_similarity_threshold": 0.88,
    "output_dir": "outputs_professional",
}


CANDIDATE_STRATEGIES = [
    {
        "name": "structure_shift",
        "instruction": (
            "Keep the same knowledge point, but change the expression structure or "
            "problem condition. Do not merely replace numbers."
        ),
    },
    {
        "name": "representation_shift",
        "instruction": (
            "Keep the same solving method, but change the representation form, such "
            "as equation to worded condition, interval to parameter condition, or "
            "formula to contextual statement when suitable."
        ),
    },
    {
        "name": "constraint_shift",
        "instruction": (
            "Keep difficulty comparable, but change the asked target or constraint "
            "wording while preserving the curriculum target."
        ),
    },
    {
        "name": "coefficient_pattern_shift",
        "instruction": (
            "Change coefficient relationships and factor pattern visibly while keeping "
            "the answer clean and verifiable."
        ),
    },
]


def load_professional_options(config_path):
    options = load_options(config_path)
    options["output_dir"] = PROFESSIONAL_DEFAULTS["output_dir"]
    options["candidate_count"] = PROFESSIONAL_DEFAULTS["candidate_count"]
    options["max_candidate_attempts"] = PROFESSIONAL_DEFAULTS["max_candidate_attempts"]
    options["selector_min_score"] = PROFESSIONAL_DEFAULTS["selector_min_score"]
    options["duplicate_similarity_threshold"] = PROFESSIONAL_DEFAULTS[
        "duplicate_similarity_threshold"
    ]

    professional_config = PROJECT_ROOT / "专业模式" / "professional_options.json"
    if professional_config.exists():
        with professional_config.open("r", encoding="utf-8-sig") as file:
            overrides = json.load(file)
        for key, value in overrides.items():
            if key in ["models", "thinking", "reasoning_effort", "token_budget", "runtime"]:
                options.setdefault(key, {}).update(value)
            else:
                options[key] = value

    return options


def read_json(path):
    with Path(path).open("r", encoding="utf-8-sig") as file:
        return json.load(file)


def read_text_if_exists(path):
    if not path:
        return ""
    file_path = Path(path)
    if not file_path.exists():
        return ""
    return file_path.read_text(encoding="utf-8")


def normalize_for_duplicate(text):
    text = (text or "").lower()
    text = "".join(ch for ch in text if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")
    return text


def char_ngrams(text, size=3):
    normalized = normalize_for_duplicate(text)
    if len(normalized) <= size:
        return {normalized} if normalized else set()
    return {
        normalized[index : index + size]
        for index in range(0, len(normalized) - size + 1)
    }


def text_similarity(left, right):
    left_grams = char_ngrams(left)
    right_grams = char_ngrams(right)
    if not left_grams or not right_grams:
        return 0.0
    return len(left_grams & right_grams) / len(left_grams | right_grams)


def candidate_question_text(candidate):
    return read_text_if_exists(candidate.get("questions"))


def max_similarity_to_texts(text, previous_texts):
    if not previous_texts:
        return 0.0
    return max(text_similarity(text, previous) for previous in previous_texts)


def issue_count(qc_result):
    total = 0
    for key in ["question_issues", "answer_issues", "style_issues"]:
        items = qc_result.get(key) or []
        if isinstance(items, list):
            total += len(items)
    return total


def major_issue_count(qc_result):
    total = 0
    for key in ["question_issues", "answer_issues", "style_issues"]:
        for item in qc_result.get(key) or []:
            if isinstance(item, dict) and item.get("severity") == "major":
                total += 1
    return total


def candidate_score(candidate):
    if candidate.get("status") == "failed":
        return -100000
    qc_path = candidate.get("qc")
    if not qc_path or not Path(qc_path).exists():
        return -10000
    qc = read_json(qc_path)
    score = qc.get("score")
    if not isinstance(score, int):
        score = 0
    passed_bonus = 1000 if qc.get("is_passed") is True else 0
    fallback_penalty = 100 if candidate.get("status") == "fallback" else 0
    duplicate_penalty = int(candidate.get("duplicate_penalty", 0))
    return (
        passed_bonus
        + score
        - major_issue_count(qc) * 120
        - issue_count(qc) * 10
        - fallback_penalty
        - duplicate_penalty
    )


def mark_duplicate_candidates(candidates, previous_selected_texts=None, threshold=0.88):
    previous_selected_texts = previous_selected_texts or []
    seen_texts = []
    for candidate in candidates:
        text = candidate_question_text(candidate)
        candidate["max_similarity_to_previous_selected"] = max_similarity_to_texts(
            text,
            previous_selected_texts,
        )
        candidate["max_similarity_to_peer_candidate"] = max_similarity_to_texts(
            text,
            seen_texts,
        )
        penalty = 0
        if candidate["max_similarity_to_previous_selected"] >= threshold:
            penalty += 600
        if candidate["max_similarity_to_peer_candidate"] >= threshold:
            penalty += 250
        candidate["duplicate_penalty"] = penalty
        seen_texts.append(text)


def select_best_candidate(
    candidates,
    selector_min_score,
    previous_selected_texts=None,
    duplicate_similarity_threshold=0.88,
):
    mark_duplicate_candidates(
        candidates,
        previous_selected_texts=previous_selected_texts,
        threshold=duplicate_similarity_threshold,
    )
    ranked = sorted(candidates, key=candidate_score, reverse=True)
    best = ranked[0]
    if best.get("status") == "failed":
        raise PipelineQualityError("专业模式所有候选均失败，无法择优输出。")

    qc = read_json(best["qc"]) if best.get("qc") and Path(best["qc"]).exists() else {}
    score = qc.get("score")
    passed = qc.get("is_passed")
    if isinstance(score, int) and score < selector_min_score:
        print(
            f"Professional Selector warning: best candidate score={score}, "
            f"below selector_min_score={selector_min_score}; keeping best available output."
        )
    if passed is not True:
        print("Professional Selector warning: best candidate did not fully pass QC.")
    return best, ranked


def build_candidate_generation_profile(base_profile, candidate_index, previous_candidate_texts):
    profile = dict(base_profile or {})
    strategy = CANDIDATE_STRATEGIES[
        (candidate_index - 1) % len(CANDIDATE_STRATEGIES)
    ]
    profile["candidate_index"] = candidate_index
    profile["candidate_strategy"] = strategy["name"]
    profile["candidate_strategy_instruction"] = strategy["instruction"]
    if previous_candidate_texts:
        profile["avoid_candidate_questions"] = previous_candidate_texts[-3:]
    return profile


def run_one_candidate(
    item,
    candidate_index,
    candidate_count_label,
    item_index,
    total_items,
    candidate_dir,
    options,
    previous_candidate_texts,
):
    print(
        f"\n=== Professional item {item_index}/{total_items}: "
        f"candidate {candidate_index}/{candidate_count_label} ==="
    )
    candidate_profile = build_candidate_generation_profile(
        options.get("generation_profile"),
        candidate_index,
        previous_candidate_texts,
    )
    try:
        result = run_single_reader_item(
            item=item,
            item_output_dir=candidate_dir,
            review_score_threshold=options["review_threshold"],
            max_review_revision_rounds=options["max_review_rounds"],
            qc_score_threshold=options["qc_threshold"],
            max_qc_revision_rounds=options["max_qc_rounds"],
            generation_profile=candidate_profile,
            skip_qc=False,
            result_store_options=options.get("result_store"),
        )
        result["candidate_index"] = candidate_index
        return result
    except Exception as error:
        fallback_status = None
        if options.get("result_store", {}).get("enabled", True):
            fallback_status = restore_recent_result(
                candidate_dir,
                result_root=options.get("result_store", {}).get("dir", "result"),
            )
        if fallback_status:
            return {
                "item_id": item.get("id"),
                "candidate_index": candidate_index,
                "status": "fallback",
                "error": str(error),
                "questions": str(candidate_dir / "generated_questions_final.md"),
                "answer_key": str(candidate_dir / "answer_key_final.md"),
                "qc": str(candidate_dir / "qc_final.json"),
                "fallback": fallback_status,
            }
        print(f"Professional candidate failed: {error}")
        return {
            "item_id": item.get("id"),
            "candidate_index": candidate_index,
            "status": "failed",
            "error": str(error),
        }


def run_professional_item(
    item,
    item_index,
    total_items,
    output_dir,
    options,
):
    item_root = Path(output_dir) / "professional_items" / f"item_{item_index:03d}"
    item_root.mkdir(parents=True, exist_ok=True)
    candidates = []
    previous_candidate_texts = []

    candidate_count = max(1, int(options.get("candidate_count", 2)))
    max_candidate_attempts = max(
        candidate_count,
        int(options.get("max_candidate_attempts", 4)),
    )
    for candidate_index in range(1, candidate_count + 1):
        candidate_dir = item_root / f"candidate_{candidate_index:02d}"
        result = run_one_candidate(
            item=item,
            candidate_index=candidate_index,
            candidate_count_label=candidate_count,
            item_index=item_index,
            total_items=total_items,
            candidate_dir=candidate_dir,
            options=options,
            previous_candidate_texts=previous_candidate_texts,
        )
        candidates.append(result)
        previous_candidate_texts.append(candidate_question_text(result))

    best, ranked = select_best_candidate(
        candidates,
        int(options.get("selector_min_score", 95)),
        previous_selected_texts=options.get("_previous_selected_texts", []),
        duplicate_similarity_threshold=float(
            options.get("duplicate_similarity_threshold", 0.88)
        ),
    )
    while (
        best.get("max_similarity_to_previous_selected", 0)
        >= float(options.get("duplicate_similarity_threshold", 0.88))
        and len(candidates) < max_candidate_attempts
    ):
        next_index = len(candidates) + 1
        print(
            "Professional Selector detected near-duplicate selected output; "
            f"running extra candidate {next_index}/{max_candidate_attempts}."
        )
        candidate_dir = item_root / f"candidate_{next_index:02d}"
        result = run_one_candidate(
            item=item,
            candidate_index=next_index,
            candidate_count_label=max_candidate_attempts,
            item_index=item_index,
            total_items=total_items,
            candidate_dir=candidate_dir,
            options=options,
            previous_candidate_texts=previous_candidate_texts,
        )
        candidates.append(result)
        previous_candidate_texts.append(candidate_question_text(result))
        best, ranked = select_best_candidate(
            candidates,
            int(options.get("selector_min_score", 95)),
            previous_selected_texts=options.get("_previous_selected_texts", []),
            duplicate_similarity_threshold=float(
                options.get("duplicate_similarity_threshold", 0.88)
            ),
        )
    selector_path = item_root / "selector_result.json"
    selector_path.write_text(
        json.dumps(
            {
                "selected_candidate": best.get("candidate_index"),
                "selected_score": candidate_score(best),
                "candidates": [
                    {
                        "candidate_index": item.get("candidate_index"),
                        "status": item.get("status"),
                        "selector_score": candidate_score(item),
                        "duplicate_penalty": item.get("duplicate_penalty", 0),
                        "max_similarity_to_previous_selected": item.get(
                            "max_similarity_to_previous_selected"
                        ),
                        "max_similarity_to_peer_candidate": item.get(
                            "max_similarity_to_peer_candidate"
                        ),
                        "qc": item.get("qc"),
                        "error": item.get("error"),
                    }
                    for item in ranked
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    best["selector"] = str(selector_path)
    print(
        f"Professional Selector chose candidate {best.get('candidate_index')} "
        f"for item {item_index}."
    )
    return best


def run_professional_pipeline(options):
    output_dir = options["output_dir"]
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    clear_stale_final_outputs(output_dir)
    professional_items_dir = Path(output_dir) / "professional_items"
    if professional_items_dir.exists():
        shutil.rmtree(professional_items_dir)

    reader_output = output_path(output_dir, "reader_result.json")
    reader_clean_input = output_path(output_dir, "reader_clean_input.md")

    print("=== Professional Reader: input normalization and item split ===")
    reader_result = run_reader(options["input"], reader_output, reader_clean_input)
    items = reader_result.get("items") or []
    selected_ids = parse_selected_item_ids(options.get("selected_item_ids"))
    if selected_ids:
        original_items = list(items)
        selected_set = set(selected_ids)
        items = [item for item in original_items if int(item.get("id", 0) or 0) in selected_set]
        if not items and len(original_items) == len(selected_ids):
            items = original_items
        print(f"Selected Reader items: {', '.join(str(item) for item in selected_ids)}")
    if not items:
        raise PipelineQualityError("Reader 没有拆出可运行题目，专业模式停止。")

    selected_results = []
    previous_selected_texts = []
    for index, item in enumerate(items, start=1):
        wait_if_paused(output_dir, f"Professional item {index}")
        item_options = dict(options)
        item_options["_previous_selected_texts"] = list(previous_selected_texts)
        selected = run_professional_item(
            item=item,
            item_index=index,
            total_items=len(items),
            output_dir=output_dir,
            options=item_options,
        )
        selected_results.append(selected)
        previous_selected_texts.append(candidate_question_text(selected))

    outputs = combine_itemwise_outputs(output_dir, selected_results)
    render_preview(
        output_dir=output_dir,
        preview_path=output_path(output_dir, "vscode_preview.md"),
    )

    professional_summary = {
        "mode": "professional",
        "candidate_count": options.get("candidate_count"),
        "max_candidate_attempts": options.get("max_candidate_attempts"),
        "selector_min_score": options.get("selector_min_score"),
        "selected_items": selected_results,
        "token_usage": get_token_usage_summary(),
    }
    Path(output_dir, "professional_summary.json").write_text(
        json.dumps(professional_summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return outputs


def build_arg_parser():
    parser = argparse.ArgumentParser(description="Professional multi-agent pipeline")
    parser.add_argument("--config", default=str(PROJECT_ROOT / "config" / "pipeline_options.json"))
    parser.add_argument("--input")
    parser.add_argument("--output-dir")
    parser.add_argument("--candidate-count", type=int)
    parser.add_argument("--max-candidate-attempts", type=int)
    parser.add_argument("--selector-min-score", type=int)
    parser.add_argument("--duplicate-similarity-threshold", type=float)
    parser.add_argument("--selected-item-ids")
    parser.add_argument("--show-options", action="store_true")
    return parser


def main():
    parser = build_arg_parser()
    args = parser.parse_args()

    options = load_professional_options(args.config)
    if args.input:
        options["input"] = args.input
    if args.output_dir:
        options["output_dir"] = args.output_dir
    if args.candidate_count is not None:
        options["candidate_count"] = args.candidate_count
    if args.max_candidate_attempts is not None:
        options["max_candidate_attempts"] = args.max_candidate_attempts
    if args.selector_min_score is not None:
        options["selector_min_score"] = args.selector_min_score
    if args.duplicate_similarity_threshold is not None:
        options["duplicate_similarity_threshold"] = args.duplicate_similarity_threshold
    if args.selected_item_ids is not None:
        options["selected_item_ids"] = args.selected_item_ids

    apply_model_options(options)
    token_budget = options.get("token_budget", {})
    set_token_budget(token_budget)
    print_effective_options(options)
    print("professional_mode:")
    print(f"  candidate_count: {options.get('candidate_count')}")
    print(f"  max_candidate_attempts: {options.get('max_candidate_attempts')}")
    print(f"  selector_min_score: {options.get('selector_min_score')}")
    print(f"  duplicate_similarity_threshold: {options.get('duplicate_similarity_threshold')}")
    print()

    if args.show_options:
        return

    try:
        run_professional_pipeline(options)
    except (ValidationError, PipelineQualityError, BudgetExceededError, RuntimeError) as error:
        status_path = write_failure_status(options["output_dir"], error)
        print()
        print(str(error))
        print(f"Failure status: {status_path}")
        token_usage = get_token_usage_summary() or {}
        print(f"Token usage before stop: {token_usage.get('used_tokens', 0)}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
