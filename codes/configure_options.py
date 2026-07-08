import json
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config" / "pipeline_options.json"


DEFAULT_OPTIONS = {
    "input": "examples/input.md",
    "output_dir": "outputs",
    "review_threshold": 90,
    "max_review_rounds": 2,
    "qc_threshold": 90,
    "max_qc_rounds": 2,
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


def load_options():
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open("r", encoding="utf-8") as file:
            loaded = json.load(file)
    else:
        loaded = {}

    return {
        **DEFAULT_OPTIONS,
        **loaded,
        "models": {
            **DEFAULT_OPTIONS["models"],
            **loaded.get("models", {}),
        },
        "thinking": {
            **DEFAULT_OPTIONS["thinking"],
            **loaded.get("thinking", {}),
        },
        "reasoning_effort": {
            **DEFAULT_OPTIONS["reasoning_effort"],
            **loaded.get("reasoning_effort", {}),
        },
        "token_budget": {
            **DEFAULT_OPTIONS["token_budget"],
            **loaded.get("token_budget", {}),
        },
        "generation_profile": {
            **DEFAULT_OPTIONS["generation_profile"],
            **loaded.get("generation_profile", {}),
        },
    }


def save_options(options):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(options, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def ask_text(label, current):
    try:
        value = input(f"{label} [{current}]: ").strip()
    except EOFError:
        return current
    return current if value == "" else value


def ask_int(label, current, min_value=0, max_value=100):
    while True:
        try:
            value = input(f"{label} [{current}]: ").strip()
        except EOFError:
            return current
        if value == "":
            return current
        try:
            parsed = int(value)
        except ValueError:
            print("Please enter an integer, or press Enter to keep the current value.")
            continue
        if parsed < min_value or parsed > max_value:
            print(f"Please enter an integer from {min_value} to {max_value}.")
            continue
        return parsed


def ask_bool(label, current):
    default_text = "y" if current else "n"
    while True:
        try:
            value = input(f"{label} y/n [{default_text}]: ").strip().lower()
        except EOFError:
            return current
        if value == "":
            return current
        if value in ["y", "yes", "1", "true", "on", "enabled"]:
            return True
        if value in ["n", "no", "0", "false", "off", "disabled"]:
            return False
        print("Please enter y or n, or press Enter to keep the current value.")


def print_options(options):
    print("\nCurrent options:")
    print(f"1. input: {options['input']}")
    print(f"2. output_dir: {options['output_dir']}")
    print(f"3. review_threshold: {options['review_threshold']}")
    print(f"4. max_review_rounds: {options['max_review_rounds']}")
    print(f"5. qc_threshold: {options['qc_threshold']}")
    print(f"6. max_qc_rounds: {options['max_qc_rounds']}")
    print(f"7. max_generated_questions: {options['max_generated_questions']}")
    print("8. generation_profile:")
    for key, value in options["generation_profile"].items():
        print(f"   {key}: {value}")
    print("9. models:")
    for key, value in options["models"].items():
        print(f"   {key}: {value}")
    print("10. thinking:")
    for key, value in options["thinking"].items():
        print(f"   {key}: {value}")
    print("11. reasoning_effort:")
    for key, value in options["reasoning_effort"].items():
        print(f"   {key}: {value}")
    print("12. token_budget:")
    for key, value in options["token_budget"].items():
        print(f"   {key}: {value}")


def main():
    options = load_options()
    print_options(options)
    print("\nEnter a new value for each option. Press Enter to keep the current value.\n")

    options["input"] = ask_text("input", options["input"])
    options["output_dir"] = ask_text("output_dir", options["output_dir"])
    options["review_threshold"] = ask_int("review_threshold", options["review_threshold"])
    options["max_review_rounds"] = ask_int("max_review_rounds", options["max_review_rounds"], 0, 10)
    options["qc_threshold"] = ask_int("qc_threshold", options["qc_threshold"])
    options["max_qc_rounds"] = ask_int("max_qc_rounds", options["max_qc_rounds"], 0, 10)
    options["max_generated_questions"] = ask_int(
        "max_generated_questions",
        options["max_generated_questions"],
        1,
        20,
    )

    generation_profile = options["generation_profile"]
    generation_profile["question_count"] = ask_int(
        "generation.question_count",
        generation_profile.get("question_count", options["max_generated_questions"]),
        1,
        50,
    )
    options["max_generated_questions"] = generation_profile["question_count"]
    generation_profile["question_count_mode"] = ask_text(
        "generation.question_count_mode fixed/match_source",
        generation_profile["question_count_mode"],
    )
    generation_profile["difficulty"] = ask_text(
        "generation.difficulty",
        generation_profile["difficulty"],
    )
    generation_profile["knowledge_points"] = ask_text(
        "generation.knowledge_points",
        generation_profile["knowledge_points"],
    )
    generation_profile["question_types"] = ask_text(
        "generation.question_types",
        generation_profile["question_types"],
    )
    generation_profile["style"] = ask_text(
        "generation.style",
        generation_profile["style"],
    )
    generation_profile["answer_detail"] = ask_text(
        "generation.answer_detail",
        generation_profile["answer_detail"],
    )
    generation_profile["allow_input_compaction"] = ask_bool(
        "generation.allow_input_compaction",
        generation_profile["allow_input_compaction"],
    )

    models = options["models"]
    models["default"] = ask_text("model.default", models["default"])
    models["decomposer"] = ask_text("model.decomposer", models["decomposer"])
    models["reviewer"] = ask_text("model.reviewer", models["reviewer"])
    models["generator"] = ask_text("model.generator", models["generator"])
    models["qc"] = ask_text("model.qc", models["qc"])

    thinking = options["thinking"]
    thinking["decomposer"] = ask_bool("thinking.decomposer", thinking["decomposer"])
    thinking["reviewer"] = ask_bool("thinking.reviewer", thinking["reviewer"])
    thinking["generator"] = ask_bool("thinking.generator", thinking["generator"])
    thinking["qc"] = ask_bool("thinking.qc", thinking["qc"])

    effort = options["reasoning_effort"]
    effort["decomposer"] = ask_text("reasoning_effort.decomposer", effort["decomposer"])
    effort["reviewer"] = ask_text("reasoning_effort.reviewer", effort["reviewer"])
    effort["generator"] = ask_text("reasoning_effort.generator", effort["generator"])
    effort["qc"] = ask_text("reasoning_effort.qc", effort["qc"])

    token_budget = options["token_budget"]
    token_budget["total_warning"] = ask_int(
        "token_budget.total_warning",
        token_budget["total_warning"],
        0,
        1000000,
    )
    token_budget["total_stop"] = ask_int(
        "token_budget.total_stop",
        token_budget["total_stop"],
        0,
        1000000,
    )
    token_budget["single_warning"] = ask_int(
        "token_budget.single_warning",
        token_budget["single_warning"],
        0,
        1000000,
    )
    token_budget["single_stop"] = ask_int(
        "token_budget.single_stop",
        token_budget["single_stop"],
        0,
        1000000,
    )

    save_options(options)
    print_options(options)
    print(f"\nSaved to: {CONFIG_PATH}")


if __name__ == "__main__":
    main()
