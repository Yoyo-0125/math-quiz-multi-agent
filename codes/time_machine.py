import json
import re
from datetime import datetime
from pathlib import Path


KEY_FILES = {
    "reader_result.json": ("Reader", "Reader split and normalized the input."),
    "reader_clean_input.md": ("Reader", "Clean readable input was produced."),
    "decomposer_final.json": ("Decomposer", "Final structure analysis was selected."),
    "reviewer_final.json": ("Reviewer", "Decomposer analysis was reviewed."),
    "generator_final.json": ("Generator", "Generator JSON output was produced."),
    "generated_questions_final.md": ("Generator", "Final generated questions were written."),
    "answer_key_final.md": ("Generator", "Final answer key was written."),
    "qc_final.json": ("QC", "Final QC result was produced."),
    "vscode_preview.md": ("Final", "VSCode preview was rendered."),
    "pipeline_failed.json": ("Failure", "Pipeline failure status was written."),
    "professional_summary.json": ("Professional", "Professional mode selected candidates."),
    "result_fallback.json": ("Fallback", "Cached result was restored."),
}

ROUND_PATTERNS = [
    (re.compile(r"decomposer_round(\d+)\.json$"), "Decomposer", "Decomposer round {round}."),
    (re.compile(r"reviewer_round(\d+)\.json$"), "Reviewer", "Reviewer round {round}."),
    (re.compile(r"generator_round(\d+)\.json$"), "Generator", "Generator round {round}."),
    (re.compile(r"generated_questions_round(\d+)\.md$"), "Generator", "Generated questions round {round}."),
    (re.compile(r"answer_key_round(\d+)\.md$"), "Generator", "Answer key round {round}."),
    (re.compile(r"qc_round(\d+)\.json$"), "QC", "QC round {round}."),
]


def read_text(path, max_chars=12000):
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except Exception:
        return ""
    if len(text) > max_chars:
        return text[:max_chars] + "\n\n[content truncated for replay]"
    return text


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except Exception:
        return default


def rel_path(path, root):
    try:
        return str(Path(path).resolve().relative_to(Path(root).resolve()))
    except Exception:
        return str(path)


def iso_time(path):
    try:
        return datetime.fromtimestamp(Path(path).stat().st_mtime).isoformat(timespec="seconds")
    except Exception:
        return ""


def preview_text(path):
    path = Path(path)
    data = read_json(path)
    if isinstance(data, dict):
        if path.name == "qc_final.json" or path.name.startswith("qc_round"):
            return "\n".join(
                [
                    f"passed: {data.get('is_passed')}",
                    f"score: {data.get('score')}",
                    f"summary: {data.get('qc_summary', '')}",
                ]
            ).strip()
        if path.name == "reviewer_final.json" or path.name.startswith("reviewer_round"):
            return "\n".join(
                [
                    f"valid: {data.get('is_valid')}",
                    f"score: {data.get('score')}",
                    f"summary: {data.get('review_summary', '')}",
                ]
            ).strip()
        if path.name.startswith("decomposer"):
            return "\n".join(
                [
                    f"topic: {data.get('topic', '')}",
                    f"grade: {data.get('grade_level', '')}",
                    f"types: {', '.join(data.get('question_types', []) if isinstance(data.get('question_types'), list) else [str(data.get('question_types', ''))])}",
                    f"count: {data.get('detected_question_count') or data.get('source_structure', {}).get('total_question_count', '')}",
                ]
            ).strip()
        if path.name.startswith("generator"):
            return "\n".join(
                [
                    "questions:",
                    str(data.get("questions_markdown", ""))[:700],
                    "",
                    "answers:",
                    str(data.get("answer_key_markdown", ""))[:700],
                ]
            ).strip()
        return json.dumps(data, ensure_ascii=False, indent=2)[:1600]
    return read_text(path, max_chars=1800)


def clean_line(text, max_chars=60):
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    text = text.replace("**", "")
    text = re.sub(r"^\s*\d+[.)]\s*", "", text)
    text = re.sub(r"^\s*[（(]\d+[）)]\s*", "", text)
    text = re.sub(r"^\s*变式\s*\d+\s*[:：]\s*", "变式：", text)
    text = text.strip("#* -")
    if len(text) > max_chars:
        text = text[: max_chars - 1].rstrip() + "…"
    return text


def first_markdown_title(text):
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            return clean_line(stripped.lstrip("#").strip())
    for line in (text or "").splitlines():
        stripped = clean_line(line)
        if stripped:
            return stripped
    return ""


def source_item_title(decomposer_result):
    if not isinstance(decomposer_result, dict):
        return ""
    source = decomposer_result.get("source_structure") or {}
    items = source.get("items") or []
    if isinstance(items, list) and len(items) == 1 and isinstance(items[0], dict):
        text = clean_line(items[0].get("text"), max_chars=72)
        if text:
            return f"单题：{text}"
    topic = clean_line(decomposer_result.get("topic"), max_chars=48)
    question_types = decomposer_result.get("question_types") or []
    if isinstance(question_types, str):
        question_types = [question_types]
    type_text = clean_line("、".join(str(item) for item in question_types[:2]), max_chars=32)
    if topic and type_text:
        return f"{topic} · {type_text}"
    return topic or type_text


def reader_title(run_dir):
    reader = read_json(Path(run_dir) / "reader_result.json", {})
    if isinstance(reader, dict):
        title = clean_line(reader.get("title") or reader.get("topic"), max_chars=64)
        if title:
            return f"整卷：{title}"
        items = reader.get("items") or []
        if isinstance(items, list) and len(items) == 1 and isinstance(items[0], dict):
            text = clean_line(items[0].get("text"), max_chars=72)
            if text:
                return f"单题：{text}"
        if isinstance(items, list) and items:
            first = items[0] if isinstance(items[0], dict) else {}
            text = clean_line(first.get("section") or first.get("text"), max_chars=50)
            if text:
                return f"整卷：{text} 等 {len(items)} 题"

    clean_input = read_text(Path(run_dir) / "reader_clean_input.md", max_chars=4000)
    title = first_markdown_title(clean_input)
    if title:
        return f"整卷：{title}"
    return ""


def generated_title(run_dir):
    questions = read_text(Path(run_dir) / "generated_questions_final.md", max_chars=2000)
    title = first_markdown_title(questions)
    if title:
        return f"结果：{title}"
    return ""


def fallback_path_title(run_dir):
    run_dir = Path(run_dir)
    if run_dir.name in {"outputs", "outputs_professional"}:
        return "当前输出记录" if run_dir.name == "outputs" else "专业模式输出记录"
    parts = list(run_dir.parts)
    if run_dir.name.startswith("candidate_"):
        return f"{run_dir.parent.name} / {run_dir.name}"
    if run_dir.name.startswith("item_"):
        return f"{run_dir.parent.name} / {run_dir.name}"
    return run_dir.name


def human_title_for_run(run_dir):
    run_dir = Path(run_dir)
    title = reader_title(run_dir)
    if title:
        return title
    title = source_item_title(read_json(run_dir / "decomposer_final.json", {}))
    if title:
        return title
    title = generated_title(run_dir)
    if title:
        return title
    return fallback_path_title(run_dir)


def node_for_file(path, root, index=None):
    path = Path(path)
    stage = None
    title = None
    for filename, (file_stage, file_title) in KEY_FILES.items():
        if path.name == filename:
            stage, title = file_stage, file_title
            break
    if stage is None:
        for pattern, pattern_stage, pattern_title in ROUND_PATTERNS:
            match = pattern.search(path.name)
            if match:
                stage = pattern_stage
                title = pattern_title.format(round=match.group(1))
                break
    if stage is None:
        return None

    kind = "thinking" if any(token in path.name for token in ["round", "debug"]) else "key"
    if path.name in {"generated_questions_final.md", "answer_key_final.md", "qc_final.json", "vscode_preview.md"}:
        kind = "result"
    if path.name in {"pipeline_failed.json", "result_fallback.json"}:
        kind = "key"
    return {
        "id": f"node-{index or 0}",
        "stage": stage,
        "title": title,
        "kind": kind,
        "path": rel_path(path, root),
        "time": iso_time(path),
        "preview": preview_text(path),
    }


def scan_run_dir(run_dir, root):
    run_dir = Path(run_dir)
    if not run_dir.exists():
        return None
    candidates = []
    for name in KEY_FILES:
        path = run_dir / name
        if path.exists():
            candidates.append(path)
    for pattern_name in [
        "decomposer_round*.json",
        "reviewer_round*.json",
        "generator_round*.json",
        "generated_questions_round*.md",
        "answer_key_round*.md",
        "qc_round*.json",
    ]:
        candidates.extend(run_dir.glob(pattern_name))
    candidates = sorted(set(candidates), key=lambda path: (path.stat().st_mtime, path.name))
    if not candidates:
        return None

    nodes = []
    for index, path in enumerate(candidates, start=1):
        node = node_for_file(path, root, index)
        if node:
            nodes.append(node)
    if not nodes:
        return None

    qc = read_json(run_dir / "qc_final.json", {})
    failure = read_json(run_dir / "pipeline_failed.json", {})
    summary = {
        "qc_score": qc.get("score") if isinstance(qc, dict) else None,
        "qc_passed": qc.get("is_passed") if isinstance(qc, dict) else None,
        "failed": bool(failure),
        "node_count": len(nodes),
    }
    title = human_title_for_run(run_dir)
    return {
        "id": rel_path(run_dir, root),
        "title": title,
        "path": rel_path(run_dir, root),
        "updated_at": iso_time(max(candidates, key=lambda path: path.stat().st_mtime)),
        "summary": summary,
        "nodes": nodes,
    }


def scan_result_records(result_root, root, limit=20):
    result_root = Path(result_root)
    if not result_root.exists():
        return []
    records = []
    for metadata_path in result_root.glob("**/metadata.json"):
        parent = metadata_path.parent
        if "verified_items" in parent.parts:
            continue
        if not (parent / "generated_questions_final.md").exists():
            continue
            record = scan_run_dir(parent, root)
            if record:
                metadata = read_json(metadata_path, {})
            record["title"] = human_title_for_run(parent)
            record["source"] = "result/" + "/".join(parent.relative_to(result_root).parts[-4:])
            record["metadata"] = metadata
            records.append(record)
    records.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
    return records[:limit]


def scan_time_machine(root, output_dirs=None, result_root=None, limit=30):
    root = Path(root)
    output_dirs = output_dirs or ["outputs", "outputs_professional"]
    runs = []
    for output in output_dirs:
        base = root / output
        record = scan_run_dir(base, root)
        if record:
            runs.append(record)
        for item_dir in sorted((base / "items").glob("item_*")) if (base / "items").exists() else []:
            record = scan_run_dir(item_dir, root)
            if record:
                runs.append(record)
        for item_dir in sorted((base / "professional_items").glob("item_*")) if (base / "professional_items").exists() else []:
            selector = item_dir / "selector_result.json"
            record = scan_run_dir(item_dir, root)
            if record:
                runs.append(record)
            for candidate_dir in sorted(item_dir.glob("candidate_*")):
                candidate = scan_run_dir(candidate_dir, root)
                if candidate:
                    candidate["candidate_label"] = f"{item_dir.name}/{candidate_dir.name}"
                    candidate["title"] = f"{candidate['title']} · {candidate_dir.name.replace('_', ' ')}"
                    if selector.exists():
                        candidate["selector"] = rel_path(selector, root)
                    runs.append(candidate)
    runs.extend(scan_result_records(root / (result_root or "result"), root, limit=limit))
    runs.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
    for index, run in enumerate(runs[:limit], start=1):
        run["index"] = index
    return {"runs": runs[:limit], "count": min(len(runs), limit)}


def load_replay(root, run_id, output_dirs=None, result_root=None):
    data = scan_time_machine(root, output_dirs=output_dirs, result_root=result_root, limit=200)
    for run in data["runs"]:
        if run["id"] == run_id:
            return run
    return None
