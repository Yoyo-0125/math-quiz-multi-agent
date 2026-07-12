import json
import hashlib
import random
import re
import shutil
from datetime import datetime
from pathlib import Path


RESULT_DIR = "result"
RESULT_FILES = [
    "generated_questions_final.md",
    "answer_key_final.md",
    "generator_final.json",
    "qc_final.json",
    "decomposer_final.json",
    "reviewer_final.json",
    "vscode_preview.md",
]


def split_numbered_blocks(markdown_text):
    blocks = {}
    current_number = None
    current_lines = []
    for line in (markdown_text or "").splitlines():
        match = re.match(r"^\s*(\d+)[.)]\s+(.*)", line)
        if match:
            if current_number is not None:
                blocks[current_number] = "\n".join(current_lines).strip()
            current_number = int(match.group(1))
            current_lines = [line]
        elif current_number is not None:
            current_lines.append(line)
    if current_number is not None:
        blocks[current_number] = "\n".join(current_lines).strip()
    return blocks


def issue_question_numbers(qc_result):
    numbers = set()
    for key in ["question_issues", "answer_issues"]:
        for issue in qc_result.get(key, []) or []:
            if not isinstance(issue, dict):
                continue
            if issue.get("severity") != "major":
                continue
            match = re.search(r"\d+", str(issue.get("question_id", "")))
            if match:
                numbers.add(int(match.group(0)))
            else:
                numbers.add("systemic")
    return numbers


def safe_segment(value, fallback="unknown"):
    text = str(value or "").strip()
    if not text:
        text = fallback
    text = re.sub(r"[\\/:*?\"<>|\s]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("._")
    return text[:80] or fallback


def read_json(path, default=None):
    path = Path(path)
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return default


def latest_existing(paths):
    existing = [Path(path) for path in paths if Path(path).exists()]
    if not existing:
        return None
    return max(existing, key=lambda path: path.stat().st_mtime)


def find_decomposer_for_output(output_dir):
    output_dir = Path(output_dir)
    candidates = [output_dir / "decomposer_final.json"]
    candidates.extend(output_dir.glob("decomposer_round*.json"))
    return latest_existing(candidates)


def result_key_from_decomposer(decomposer_result):
    question_types = decomposer_result.get("question_types") or ["unknown_type"]
    if isinstance(question_types, str):
        question_types = [question_types]
    type_key = "_".join(safe_segment(item, "type") for item in question_types[:3])
    return {
        "topic": safe_segment(decomposer_result.get("topic"), "unknown_topic"),
        "grade": safe_segment(decomposer_result.get("grade_level"), "unknown_grade"),
        "question_types": safe_segment(type_key, "unknown_type"),
    }


def decomposer_fingerprint(decomposer_result):
    exact_payload = {
        "topic": decomposer_result.get("topic"),
        "subtopics": decomposer_result.get("subtopics"),
        "grade_level": decomposer_result.get("grade_level"),
        "question_types": decomposer_result.get("question_types"),
        "format_structure": decomposer_result.get("format_structure"),
        "difficulty_distribution": decomposer_result.get("difficulty_distribution"),
        "style_features": decomposer_result.get("style_features"),
        "source_structure": decomposer_result.get("source_structure"),
    }
    text = json.dumps(exact_payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def make_record_dir(result_root, decomposer_result):
    key = result_key_from_decomposer(decomposer_result)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return (
        Path(result_root)
        / key["topic"]
        / key["question_types"]
        / key["grade"]
        / timestamp
    )


def make_verified_item_dir(result_root, decomposer_result, question_number):
    key = result_key_from_decomposer(decomposer_result)
    return (
        Path(result_root)
        / "verified_items"
        / key["topic"]
        / key["question_types"]
        / key["grade"]
        / decomposer_fingerprint(decomposer_result)
        / "items"
        / f"q{question_number:03d}"
        / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    )


def make_verified_set_dir(result_root, decomposer_result):
    key = result_key_from_decomposer(decomposer_result)
    return (
        Path(result_root)
        / "verified_items"
        / key["topic"]
        / key["question_types"]
        / key["grade"]
        / decomposer_fingerprint(decomposer_result)
    )


def qc_weight(qc_result):
    try:
        score = int(qc_result.get("score", 0))
    except (TypeError, ValueError):
        score = 0
    if qc_result.get("is_passed") is True:
        score = max(score, 90)
    return max(1, min(score, 100))


def save_verified_items(
    decomposer_result,
    questions_markdown,
    answer_key_markdown,
    qc_result,
    result_root=RESULT_DIR,
    source_round=None,
):
    question_blocks = split_numbered_blocks(questions_markdown)
    answer_blocks = split_numbered_blocks(answer_key_markdown)
    if not question_blocks or not answer_blocks:
        return []

    bad_numbers = issue_question_numbers(qc_result)
    if "systemic" in bad_numbers:
        return []

    set_dir = make_verified_set_dir(result_root, decomposer_result)
    set_dir.mkdir(parents=True, exist_ok=True)
    set_metadata = {
        "note": "qc_verified_item_pool",
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "fingerprint": decomposer_fingerprint(decomposer_result),
        "key": result_key_from_decomposer(decomposer_result),
        "decomposer_result": decomposer_result,
    }
    (set_dir / "metadata.json").write_text(
        json.dumps(set_metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    saved_dirs = []
    weight = qc_weight(qc_result)
    for number, question_text in sorted(question_blocks.items()):
        if number in bad_numbers:
            continue
        answer_text = answer_blocks.get(number)
        if not answer_text:
            continue
        item_dir = make_verified_item_dir(result_root, decomposer_result, number)
        item_dir.mkdir(parents=True, exist_ok=True)
        (item_dir / "question.md").write_text(question_text + "\n", encoding="utf-8")
        (item_dir / "answer.md").write_text(answer_text + "\n", encoding="utf-8")
        metadata = {
            "note": "qc_verified_single_item",
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "question_number": number,
            "source_round": source_round,
            "qc_score": qc_result.get("score"),
            "qc_passed": qc_result.get("is_passed"),
            "weight": weight,
            "fingerprint": decomposer_fingerprint(decomposer_result),
            "key": result_key_from_decomposer(decomposer_result),
            "detected_question_count": decomposer_result.get("detected_question_count"),
        }
        (item_dir / "metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        saved_dirs.append(item_dir)
    return saved_dirs


def iter_verified_item_records(set_dir, question_number=None):
    set_dir = Path(set_dir)
    items_root = set_dir / "items"
    if not items_root.exists():
        return []
    pattern = "q*" if question_number is None else f"q{int(question_number):03d}"
    records = []
    for q_dir in items_root.glob(pattern):
        for metadata_path in q_dir.glob("*/metadata.json"):
            metadata = read_json(metadata_path, {})
            question_path = metadata_path.parent / "question.md"
            answer_path = metadata_path.parent / "answer.md"
            if question_path.exists() and answer_path.exists():
                records.append(
                    {
                        "dir": metadata_path.parent,
                        "metadata": metadata,
                        "question_path": question_path,
                        "answer_path": answer_path,
                        "weight": max(1, int(metadata.get("weight", 1) or 1)),
                    }
                )
    return records


def weighted_choice(records):
    if not records:
        return None
    return random.choices(records, weights=[record["weight"] for record in records], k=1)[0]


def find_verified_set_for_decomposer(decomposer_result, result_root=RESULT_DIR):
    set_dir = make_verified_set_dir(result_root, decomposer_result)
    return set_dir if set_dir.exists() else None


def restore_weighted_verified_items(output_dir, decomposer_result, result_root=RESULT_DIR):
    set_dir = find_verified_set_for_decomposer(decomposer_result, result_root)
    if not set_dir:
        return None

    source_count = (
        decomposer_result.get("source_structure", {}).get("total_question_count")
        or decomposer_result.get("detected_question_count")
        or 1
    )
    try:
        source_count = max(1, int(source_count))
    except (TypeError, ValueError):
        source_count = 1

    question_lines = []
    answer_lines = []
    chosen = []
    for number in range(1, source_count + 1):
        records = iter_verified_item_records(set_dir, question_number=number)
        if not records:
            records = iter_verified_item_records(set_dir)
        record = weighted_choice(records)
        if not record:
            continue
        question_text = record["question_path"].read_text(encoding="utf-8").strip()
        answer_text = record["answer_path"].read_text(encoding="utf-8").strip()
        question_text = re.sub(r"^\s*\d+[.)]\s*", f"{number}. ", question_text, count=1)
        answer_text = re.sub(r"^\s*\d+[.)]\s*", f"{number}. ", answer_text, count=1)
        question_lines.append(question_text)
        answer_lines.append(answer_text)
        chosen.append(
            {
                "question_number": number,
                "record_dir": str(record["dir"]),
                "weight": record["weight"],
            }
        )

    if not question_lines or not answer_lines:
        return None

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "generated_questions_final.md").write_text(
        "\n\n".join(question_lines) + "\n",
        encoding="utf-8",
    )
    (output_dir / "answer_key_final.md").write_text(
        "\n\n".join(answer_lines) + "\n",
        encoding="utf-8",
    )
    fallback_qc = {
        "is_passed": True,
        "score": None,
        "question_issues": [],
        "answer_issues": [],
        "style_issues": [],
        "revision_instructions": [],
        "qc_summary": "Restored by weighted verified-item result library.",
        "fallback": True,
    }
    (output_dir / "qc_final.json").write_text(
        json.dumps(fallback_qc, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    status = {
        "is_fallback": True,
        "mode": "weighted_verified_items",
        "restored_from": str(set_dir),
        "restored_at": datetime.now().isoformat(timespec="seconds"),
        "chosen_items": chosen,
    }
    (output_dir / "result_fallback.json").write_text(
        json.dumps(status, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return status


def save_success_record(
    output_dir,
    result_root=RESULT_DIR,
    options=None,
    note="pipeline_success",
):
    output_dir = Path(output_dir)
    decomposer_path = output_dir / "decomposer_final.json"
    decomposer_result = read_json(decomposer_path, {})
    if not decomposer_result:
        return None

    record_dir = make_record_dir(result_root, decomposer_result)
    record_dir.mkdir(parents=True, exist_ok=True)

    copied = []
    for filename in RESULT_FILES:
        source = output_dir / filename
        if source.exists():
            shutil.copyfile(source, record_dir / filename)
            copied.append(filename)

    metadata = {
        "note": note,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "source_output_dir": str(output_dir),
        "key": result_key_from_decomposer(decomposer_result),
        "copied_files": copied,
        "options": options or {},
        "detected_question_count": decomposer_result.get("detected_question_count"),
        "source_structure": decomposer_result.get("source_structure", {}),
    }
    (record_dir / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return record_dir


def iter_records(result_root=RESULT_DIR):
    root = Path(result_root)
    if not root.exists():
        return []
    records = []
    for metadata_path in root.rglob("metadata.json"):
        try:
            relative_parts = metadata_path.relative_to(root).parts
        except ValueError:
            relative_parts = metadata_path.parts
        if relative_parts and relative_parts[0] == "verified_items":
            continue
        record_dir = metadata_path.parent
        metadata = read_json(metadata_path, {})
        records.append((record_dir, metadata))
    return records


def record_sort_key(record):
    record_dir, metadata = record
    created_at = metadata.get("created_at", "")
    try:
        timestamp = datetime.fromisoformat(created_at).timestamp()
    except Exception:
        timestamp = record_dir.stat().st_mtime
    return timestamp


def find_recent_record(output_dir, result_root=RESULT_DIR):
    records = iter_records(result_root)
    if not records:
        return None

    decomposer_path = find_decomposer_for_output(output_dir)
    if decomposer_path:
        current = read_json(decomposer_path, {})
        if current:
            key = result_key_from_decomposer(current)
            exact = [
                record
                for record in records
                if record[1].get("key", {}).get("topic") == key["topic"]
                and record[1].get("key", {}).get("question_types") == key["question_types"]
                and record[1].get("key", {}).get("grade") == key["grade"]
            ]
            if exact:
                return max(exact, key=record_sort_key)[0]

            same_topic = [
                record
                for record in records
                if record[1].get("key", {}).get("topic") == key["topic"]
            ]
            if same_topic:
                return max(same_topic, key=record_sort_key)[0]

    return max(records, key=record_sort_key)[0]


def restore_record(record_dir, output_dir):
    record_dir = Path(record_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    restored = []
    for filename in RESULT_FILES:
        source = record_dir / filename
        if source.exists():
            shutil.copyfile(source, output_dir / filename)
            restored.append(filename)

    status = {
        "is_fallback": True,
        "restored_from": str(record_dir),
        "restored_at": datetime.now().isoformat(timespec="seconds"),
        "restored_files": restored,
    }
    (output_dir / "result_fallback.json").write_text(
        json.dumps(status, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return status


def restore_recent_result(output_dir, result_root=RESULT_DIR, allow_global_record=True):
    decomposer_path = find_decomposer_for_output(output_dir)
    if decomposer_path:
        decomposer_result = read_json(decomposer_path, {})
        if decomposer_result:
            weighted_status = restore_weighted_verified_items(
                output_dir,
                decomposer_result,
                result_root=result_root,
            )
            if weighted_status:
                return weighted_status

    if not allow_global_record:
        return None

    record_dir = find_recent_record(output_dir, result_root)
    if not record_dir:
        return None
    return restore_record(record_dir, output_dir)
