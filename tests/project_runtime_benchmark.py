import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_DIR = PROJECT_ROOT / "examples" / "coverage_rounds"
OUTPUT_ROOT = PROJECT_ROOT / "outputs" / "runtime_benchmark"
REPORT_MD_PATH = PROJECT_ROOT / "docs" / "project_runtime_report.md"
REPORT_JSON_PATH = PROJECT_ROOT / "docs" / "project_runtime_report.json"

RUNTIME_LIMIT_SECONDS = 360


def sample_paths(max_rounds=None, start_index=1):
    paths = sorted(
        path for path in SAMPLE_DIR.glob("round_*.md")
        if not path.name.endswith("_answer_key.md")
    )
    paths = paths[max(0, start_index - 1):]
    if max_rounds is not None:
        return paths[:max_rounds]
    return paths


def run_one(sample_path, timeout_seconds, skip_qc=False, review_threshold=90, qc_threshold=90):
    output_dir = OUTPUT_ROOT / sample_path.stem
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "codes/run_pipeline.py",
        "--input",
        str(sample_path.relative_to(PROJECT_ROOT)),
        "--output-dir",
        str(output_dir.relative_to(PROJECT_ROOT)),
        "--question-count-mode",
        "match_source",
        "--review-threshold",
        str(review_threshold),
        "--qc-threshold",
        str(qc_threshold),
        "--max-review-rounds",
        "2",
        "--max-qc-rounds",
        "2",
    ]
    if skip_qc:
        command.append("--skip-qc")

    start = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout_seconds,
        )
        timed_out = False
        output = completed.stdout
        return_code = completed.returncode
    except subprocess.TimeoutExpired as error:
        timed_out = True
        output = (error.stdout or "") if isinstance(error.stdout, str) else ""
        return_code = None

    elapsed = round(time.monotonic() - start, 2)
    generated_questions = output_dir / "generated_questions_final.md"
    answer_key = output_dir / "answer_key_final.md"
    failure_status = output_dir / "pipeline_failed.json"
    return {
        "input": str(sample_path.relative_to(PROJECT_ROOT)),
        "output_dir": str(output_dir.relative_to(PROJECT_ROOT)),
        "command": " ".join(command),
        "elapsed_seconds": elapsed,
        "runtime_limit_seconds": timeout_seconds,
        "within_runtime_limit": elapsed <= timeout_seconds and not timed_out,
        "return_code": return_code,
        "timed_out": timed_out,
        "generated_questions_exists": generated_questions.exists(),
        "answer_key_exists": answer_key.exists(),
        "failure_status_exists": failure_status.exists(),
        "stdout_tail": "\n".join(output.splitlines()[-30:]),
    }


def write_reports(report):
    REPORT_JSON_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    lines = [
        "Project Runtime Report",
        "",
        f"runtime_limit_seconds: {report['runtime_limit_seconds']}",
        f"rounds_run: {report['rounds_run']}",
        f"all_within_limit: {report['all_within_limit']}",
        f"all_outputs_exist: {report['all_outputs_exist']}",
        "",
        "rounds:",
    ]
    for item in report["rounds"]:
        lines.append(
            "- "
            f"{item['input']}: seconds={item['elapsed_seconds']}, "
            f"within_limit={item['within_runtime_limit']}, "
            f"return_code={item['return_code']}, "
            f"questions={item['generated_questions_exists']}, "
            f"answers={item['answer_key_exists']}"
        )
    REPORT_MD_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(
        description="Measure actual project runtime for coverage-round inputs."
    )
    parser.add_argument("--max-rounds", type=int, default=None)
    parser.add_argument("--start-index", type=int, default=1)
    parser.add_argument("--timeout-seconds", type=int, default=RUNTIME_LIMIT_SECONDS)
    parser.add_argument("--skip-qc", action="store_true")
    parser.add_argument("--review-threshold", type=int, default=90)
    parser.add_argument("--qc-threshold", type=int, default=90)
    args = parser.parse_args()

    rounds = []
    for path in sample_paths(args.max_rounds, args.start_index):
        print(f"Running project benchmark: {path.relative_to(PROJECT_ROOT)}")
        rounds.append(
            run_one(
                path,
                args.timeout_seconds,
                skip_qc=args.skip_qc,
                review_threshold=args.review_threshold,
                qc_threshold=args.qc_threshold,
            )
        )

    report = {
        "runtime_limit_seconds": args.timeout_seconds,
        "skip_qc": args.skip_qc,
        "review_threshold": args.review_threshold,
        "qc_threshold": args.qc_threshold,
        "rounds_run": len(rounds),
        "all_within_limit": all(item["within_runtime_limit"] for item in rounds),
        "all_outputs_exist": all(
            item["generated_questions_exists"] and item["answer_key_exists"]
            for item in rounds
        ),
        "rounds": rounds,
    }
    write_reports(report)
    print(json.dumps(report, ensure_ascii=False, indent=2))

    if not report["all_within_limit"]:
        raise SystemExit("project runtime exceeded limit")
    if not report["all_outputs_exist"]:
        raise SystemExit("project output missing")


if __name__ == "__main__":
    main()
