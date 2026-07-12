import html
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
import cgi
import hashlib
import mimetypes
import zipfile
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

CODE_DIR = Path(__file__).resolve().parent
if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))

from ocr_agent import run_ocr
from run_pipeline import DEFAULT_OPTIONS as PIPELINE_DEFAULT_OPTIONS
from run_pipeline import PAUSE_FILENAME
from time_machine import load_replay, scan_time_machine
from math_latex_sanitizer import sanitize_math_markdown


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "config" / "pipeline_options.json"
HOST = "127.0.0.1"
PORT = 8501
APP_PATH = "/728-2035-99/"
CODE_TIME_MACHINE_DIR = PROJECT_ROOT / "outputs" / "code_time_machine"
LAST_COMMAND = {"cmd": "", "code": "", "stdout": "", "stderr": ""}
RUN_LOCK = threading.Lock()
PROCESS_LOCK = threading.Lock()
CURRENT_PROCESS = None
RUN_STATE = {
    "running": False,
    "stage": "Idle",
    "round": "-",
    "progress": 0,
    "step_progress": 0,
    "agent_progress": {
        "Reader": 0,
        "Decomposer": 0,
        "Reviewer": 0,
        "Generator": 0,
        "QC": 0,
    },
    "started_at": "",
    "started_ts": 0,
    "finished_at": "",
    "finished_ts": 0,
    "returncode": "",
    "cmd": "",
    "log": "",
    "error_message": "",
    "last_progress_at": 0,
    "stage_started_ts": 0,
    "last_heartbeat_at": 0,
    "item_index": 0,
    "item_total": 0,
    "item_completed": 0,
    "pid": "",
    "stop_requested": False,
    "paused": False,
    "paused_at": "",
    "pause_owner": "",
}

WATCHED_CODE_PATTERNS = ["codes/*.py", "tests/*.py", "flash模式/*.py", "专业模式/*.py", "*.bat"]


AGENT_ORDER = ["Reader", "Decomposer", "Reviewer", "Generator", "QC"]
AGENT_NAME_MAP = {agent.lower(): agent for agent in AGENT_ORDER}
AGENT_EXPECTED_SECONDS = {
    "Reader": 35,
    "Decomposer": 45,
    "Reviewer": 35,
    "Generator": 260,
    "QC": 180,
}
TOTAL_EXPECTED_SECONDS = sum(AGENT_EXPECTED_SECONDS.values())
ITEM_STAGE_ORDER = ["Decomposer", "Reviewer", "Generator", "QC"]
ITEM_STAGE_EXPECTED_SECONDS = {
    agent: AGENT_EXPECTED_SECONDS[agent]
    for agent in ITEM_STAGE_ORDER
}
ITEM_EXPECTED_SECONDS = sum(ITEM_STAGE_EXPECTED_SECONDS.values())
INCOMPLETE_STAGE_CAP = 97
ENGINE_COMMANDS = {
    "flash": ["flash模式", "run_flash.py"],
    "pro": ["专业模式", "professional_pipeline.py"],
}
STAGE_HEARTBEATS = {
    "Reader": [
        "Reader is normalizing the input before analysis.",
        "Reader is checking question boundaries and math text.",
        "Reader is preparing a clean readable input for later agents.",
    ],
    "Generator": [
        "Generator is still working; waiting for model response.",
        "Generator is drafting similar questions and answer key.",
        "Generator is processing LaTeX/math structure; no action needed.",
        "Generator is taking longer than usual, but the process is still alive.",
    ],
    "QC": [
        "QC is still checking generated questions and answers.",
        "QC is reviewing math correctness and formatting.",
        "QC is preparing revision feedback if needed.",
        "QC is taking longer than usual, but the process is still alive.",
    ],
}


def expected_time_progress(elapsed_seconds, expected_seconds):
    expected_seconds = max(float(expected_seconds or 1), 1.0)
    elapsed_seconds = max(float(elapsed_seconds or 0), 0.0)
    ratio = elapsed_seconds / expected_seconds
    if ratio <= 1:
        return int(min(85, ratio * 85))
    over_ratio = min(ratio - 1, 1)
    return int(min(INCOMPLETE_STAGE_CAP, 85 + over_ratio * (INCOMPLETE_STAGE_CAP - 85)))


def current_item_progress_fraction(stage, step_percent):
    if stage not in ITEM_STAGE_ORDER:
        return 0.0
    completed_seconds = 0.0
    for agent in ITEM_STAGE_ORDER:
        if agent == stage:
            completed_seconds += ITEM_STAGE_EXPECTED_SECONDS[agent] * min(step_percent, 100) / 100
            break
        completed_seconds += ITEM_STAGE_EXPECTED_SECONDS[agent]
    return max(0.0, min(0.99, completed_seconds / max(ITEM_EXPECTED_SECONDS, 1)))


def itemwise_total_progress(item_completed, item_total, stage, step_percent):
    item_total = int(item_total or 0)
    if item_total <= 0:
        return None
    item_completed = max(0, min(int(item_completed or 0), item_total))
    fraction = current_item_progress_fraction(stage, step_percent)
    percent = ((item_completed + fraction) / item_total) * 100
    return min(99, int(percent))


def read_text(path, default=""):
    path = Path(path)
    if not path.exists():
        return default
    return path.read_text(encoding="utf-8")


def is_fresh_output(input_path, output_path):
    input_path = Path(input_path)
    output_path = Path(output_path)
    if not output_path.exists():
        return False
    try:
        if input_path.exists() and output_path.stat().st_mtime < input_path.stat().st_mtime:
            return False
    except OSError:
        return False
    return True


def read_fresh_output(input_path, output_path, stale_message="输入已更新，旧输出已隐藏。请重新运行 pipeline。"):
    return read_text(output_path)


def write_text(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as file:
        file.write(content)


def normalize_input_text(content):
    return display_text(content).replace("\r\n", "\n").replace("\r", "\n")


def display_text(value):
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, indent=2)
    if value is None:
        return ""
    return str(value)


def read_json(path, default=None):
    path = Path(path)
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError:
        return default


def write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as file:
        file.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def clear_output_artifacts(output_dir):
    output_dir = Path(output_dir)
    filenames = [
        "generated_questions_final.md",
        "answer_key_final.md",
        "qc_final.json",
        "pipeline_failed.json",
        "vscode_preview.md",
        "preview.md",
        "question_bank_similar.md",
        "question_bank_similar.json",
        "generator_final.json",
        "decomposer_final.json",
        "reviewer_final.json",
        "reader_result.json",
        "reader_clean_input.md",
        PAUSE_FILENAME,
    ]
    for filename in filenames:
        try:
            path = output_dir / filename
            if path.exists():
                path.unlink()
        except OSError:
            pass
    items_dir = output_dir / "items"
    try:
        if items_dir.exists():
            shutil.rmtree(items_dir)
    except OSError:
        pass


def project_path(value):
    path = Path(value or "")
    if path.is_absolute():
        return path
    return PROJECT_ROOT / path


def rel_path(path):
    try:
        return str(Path(path).resolve().relative_to(PROJECT_ROOT))
    except Exception:
        return str(path)


def safe_upload_name(filename):
    name = Path(filename or "upload.png").name
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return cleaned or "upload.png"


def upload_suffix_from_content_type(content_type):
    content_type = (content_type or "").split(";", 1)[0].strip().lower()
    mapping = {
        "application/pdf": ".pdf",
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/webp": ".webp",
        "image/bmp": ".bmp",
        "image/tiff": ".tiff",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
        "application/msword": ".doc",
    }
    return mapping.get(content_type, "")


def safe_upload_filename(field, index):
    filename = safe_upload_name(getattr(field, "filename", "") or f"upload_{index}")
    path = Path(filename)
    if not path.suffix:
        suffix = upload_suffix_from_content_type(getattr(field, "type", ""))
        if suffix:
            filename += suffix
    return f"{index:02d}_{filename}"


def is_word_path(path):
    return Path(path).suffix.lower() in {".docx", ".doc"}


def is_ocr_source_path(path):
    suffix = Path(path).suffix.lower()
    return suffix == ".pdf" or suffix in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}


def extract_docx_text(path):
    path = Path(path)
    if path.suffix.lower() == ".doc":
        raise ValueError("Old .doc files are not supported yet. Please save the document as .docx and drag it again.")
    paragraphs = []
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    with zipfile.ZipFile(path, "r") as archive:
        xml_bytes = archive.read("word/document.xml")
    root = ET.fromstring(xml_bytes)
    for para in root.findall(".//w:p", ns):
        parts = []
        for node in para.iter():
            if node.tag == f"{{{ns['w']}}}t" and node.text:
                parts.append(node.text)
            elif node.tag == f"{{{ns['w']}}}tab":
                parts.append("\t")
            elif node.tag == f"{{{ns['w']}}}br":
                parts.append("\n")
        text = "".join(parts).strip()
        if text:
            paragraphs.append(text)
    return "\n\n".join(paragraphs).strip()


def safe_project_file(value):
    raw = urllib.parse.unquote(str(value or ""))
    path = project_path(raw)
    resolved = path.resolve()
    root = PROJECT_ROOT.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError("File path is outside project root.")
    if not resolved.exists() or not resolved.is_file():
        raise ValueError("File not found.")
    return resolved


def blocks_for_ocr_node(node, structured):
    pages = structured.get("pages", []) if isinstance(structured, dict) else []
    blocks = []
    for page in pages:
        blocks.extend(page.get("blocks", []))
    blocks.sort(key=lambda item: (item.get("page", 0), item.get("order", 0), item.get("block_id", "")))
    start = node.get("start") or {}
    end = node.get("end") or start
    start_key = (start.get("page"), start.get("order"), start.get("block_id"))
    end_key = (end.get("page"), end.get("order"), end.get("block_id"))
    return [
        block
        for block in blocks
        if start_key <= (block.get("page"), block.get("order"), block.get("block_id")) <= end_key
    ]


def ocr_question_options(result):
    tree_path = result.get("question_tree_output")
    structured_path = result.get("structured_output")
    tree = read_json(tree_path, {}) if tree_path else {}
    structured = read_json(structured_path, {}) if structured_path else {}
    nodes = tree.get("nodes", []) if isinstance(tree, dict) else []
    options = []
    for node in nodes:
        blocks = blocks_for_ocr_node(node, structured)
        text = "\n".join(block.get("text", "").strip() for block in blocks if block.get("text", "").strip()).strip()
        if not text:
            text = f"{node.get('label_raw', '')} {node.get('title_text', '')}".strip()
        options.append(
            {
                "id": node.get("node_id", ""),
                "number": (node.get("path") or [len(options) + 1])[0],
                "label": f"{'.'.join(str(item) for item in node.get('path', []))} {node.get('title_text') or node.get('label_raw') or ''}".strip(),
                "level": node.get("level", ""),
                "path": node.get("path", []),
                "pages": [entry.get("page") for entry in node.get("bbox_pages", [])],
                "text": text,
                "warnings": node.get("warnings", []),
            }
        )
    return options


def code_signature():
    latest_mtime = 0
    latest_name = ""
    file_count = 0
    for pattern in WATCHED_CODE_PATTERNS:
        for path in PROJECT_ROOT.glob(pattern):
            if not path.is_file():
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            file_count += 1
            if mtime > latest_mtime:
                latest_mtime = mtime
                latest_name = rel_path(path)
    return {
        "version": f"{int(latest_mtime)}-{file_count}",
        "latest_file": latest_name,
        "latest_mtime": int(latest_mtime),
    }


def watched_code_files():
    files = []
    seen = set()
    for pattern in WATCHED_CODE_PATTERNS:
        for path in PROJECT_ROOT.glob(pattern):
            if not path.is_file():
                continue
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            files.append(path)
    return sorted(files, key=lambda item: rel_path(item).lower())


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_snapshot_id(value):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(value)).strip("._") or "snapshot"


def code_snapshot_manifest_path(snapshot_id):
    return CODE_TIME_MACHINE_DIR / snapshot_id / "manifest.json"


def list_code_snapshots(limit=20):
    CODE_TIME_MACHINE_DIR.mkdir(parents=True, exist_ok=True)
    snapshots = []
    for manifest_path in CODE_TIME_MACHINE_DIR.glob("*/manifest.json"):
        manifest = read_json(manifest_path, {})
        if not isinstance(manifest, dict):
            continue
        manifest["id"] = manifest.get("id") or manifest_path.parent.name
        snapshots.append(manifest)
    snapshots.sort(key=lambda item: item.get("created_ts", 0), reverse=True)
    return [compact_code_snapshot(item) for item in snapshots[:limit]]


def compact_code_snapshot(manifest):
    return {
        "id": manifest.get("id", ""),
        "version": manifest.get("version", ""),
        "reason": manifest.get("reason", ""),
        "created_ts": manifest.get("created_ts", 0),
        "created_at": manifest.get("created_at", ""),
        "latest_file": manifest.get("latest_file", ""),
        "latest_mtime": manifest.get("latest_mtime", 0),
        "file_count": manifest.get("file_count", 0),
    }


def create_code_snapshot(reason="manual"):
    signature = code_signature()
    version = signature.get("version") or str(int(time.time()))
    existing = next(
        (item for item in list_code_snapshots(limit=80) if item.get("version") == version),
        None,
    )
    if existing:
        return existing

    created_ts = int(time.time())
    snapshot_id = safe_snapshot_id(f"{created_ts}_{version}")
    snapshot_dir = CODE_TIME_MACHINE_DIR / snapshot_id
    files_dir = snapshot_dir / "files"
    files = []
    for source in watched_code_files():
        relative = rel_path(source)
        target = files_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        files.append(
            {
                "path": relative,
                "sha256": file_sha256(source),
                "size": source.stat().st_size,
                "mtime": int(source.stat().st_mtime),
            }
        )
    manifest = {
        "id": snapshot_id,
        "version": version,
        "reason": reason,
        "created_ts": created_ts,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(created_ts)),
        "latest_file": signature.get("latest_file", ""),
        "latest_mtime": signature.get("latest_mtime", 0),
        "file_count": len(files),
        "files": files,
    }
    write_json(snapshot_dir / "manifest.json", manifest)
    return compact_code_snapshot(manifest)


def ensure_current_code_snapshot():
    try:
        return create_code_snapshot(reason="auto")
    except Exception as exc:
        return {"error": str(exc), "snapshots": list_code_snapshots(limit=20)}


def code_time_machine_payload():
    current = ensure_current_code_snapshot()
    return {
        "current": compact_code_snapshot(current) if current and not current.get("error") else current,
        "snapshots": list_code_snapshots(limit=30),
    }


def restore_code_snapshot(snapshot_id):
    snapshot_id = safe_snapshot_id(snapshot_id)
    manifest_path = code_snapshot_manifest_path(snapshot_id)
    manifest = read_json(manifest_path, {})
    if not manifest:
        raise ValueError("Code snapshot was not found.")
    files_dir = manifest_path.parent / "files"
    restored = []
    for item in manifest.get("files", []):
        relative = item.get("path", "")
        if not relative:
            continue
        source = files_dir / relative
        target = PROJECT_ROOT / relative
        if not source.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        restored.append(relative)
    return {
        "id": manifest.get("id", snapshot_id),
        "version": manifest.get("version", ""),
        "restored": restored,
        "restored_count": len(restored),
    }


def load_options():
    defaults = json.loads(json.dumps(PIPELINE_DEFAULT_OPTIONS))
    current = read_json(CONFIG_PATH, {})
    for key, value in current.items():
        if isinstance(value, dict) and isinstance(defaults.get(key), dict):
            defaults[key].update(value)
        else:
            defaults[key] = value
    return defaults


def option_from_form(form):
    options = load_options()
    profile = options["generation_profile"]
    models = options["models"]
    thinking = options["thinking"]
    effort = options["reasoning_effort"]
    budget = options["token_budget"]
    runtime = options["runtime"]
    result_store = options["result_store"]

    options["input"] = form.get("input", options["input"])
    options["output_dir"] = form.get("output_dir", options["output_dir"])
    options["review_threshold"] = int(form.get("review_threshold", options["review_threshold"]))
    options["max_review_rounds"] = int(form.get("max_review_rounds", options["max_review_rounds"]))
    options["qc_threshold"] = int(form.get("qc_threshold", options["qc_threshold"]))
    options["max_qc_rounds"] = int(form.get("max_qc_rounds", options["max_qc_rounds"]))

    profile["question_count_mode"] = form.get("question_count_mode", profile["question_count_mode"])
    profile["question_count"] = int(form.get("question_count", profile["question_count"]))
    profile["difficulty"] = form.get("difficulty", profile["difficulty"])
    profile["knowledge_points"] = form.get("knowledge_points", profile["knowledge_points"])
    profile["question_types"] = form.get("question_types", profile["question_types"])
    profile["style"] = form.get("style", profile["style"])
    profile["answer_detail"] = form.get("answer_detail", profile["answer_detail"])
    profile["allow_input_compaction"] = form.get("allow_input_compaction") == "on"

    for name in ["default", "reader", "decomposer", "reviewer", "generator", "qc"]:
        models[name] = form.get(f"model_{name}", models.get(name, ""))
    for name in ["reader", "decomposer", "reviewer", "generator", "qc"]:
        thinking[name] = form.get(f"thinking_{name}") == "on"
        effort[name] = form.get(f"effort_{name}", effort.get(name, "medium"))
    for key in ["total_warning", "total_stop", "single_warning", "single_stop"]:
        budget[key] = int(form.get(f"budget_{key}", budget.get(key, 0)))
    runtime["request_timeout_seconds"] = int(
        form.get("runtime_request_timeout_seconds", runtime.get("request_timeout_seconds", 90))
    )
    runtime["generator_warning_seconds"] = int(
        form.get("runtime_generator_warning_seconds", runtime.get("generator_warning_seconds", 120))
    )
    runtime["generator_timeout_seconds"] = int(
        form.get("runtime_generator_timeout_seconds", runtime.get("generator_timeout_seconds", 180))
    )
    runtime["fallback_on_error"] = form.get("runtime_fallback_on_error") == "on"
    runtime["fallback_on_timeout"] = form.get("runtime_fallback_on_timeout") == "on"
    result_store["enabled"] = form.get("result_store_enabled") == "on"
    result_store["dir"] = form.get("result_store_dir", result_store.get("dir", "result"))

    options["max_generated_questions"] = int(profile["question_count"])
    options["selected_item_ids"] = form.get("selected_item_ids", options.get("selected_item_ids", ""))
    return options


def engine_mode_from_form(form):
    return "pro" if form.get("engine_mode") == "pro" else "flash"


def pipeline_command(engine_mode, options=None):
    script_parts = ENGINE_COMMANDS.get(engine_mode, ENGINE_COMMANDS["flash"])
    script_path = PROJECT_ROOT.joinpath(*script_parts)
    if not script_path.exists():
        script_path = PROJECT_ROOT / "codes" / "run_pipeline.py"
    command = [sys.executable, "-u", str(script_path)]
    if options and options.get("selected_item_ids"):
        command.extend(["--selected-item-ids", str(options.get("selected_item_ids"))])
    if engine_mode == "pro" and options:
        command.extend(["--output-dir", str(options.get("output_dir", "outputs"))])
    return command


def run_command(args, timeout=None):
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    completed = subprocess.run(
        args,
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=env,
    )
    LAST_COMMAND.update(
        {
            "cmd": " ".join(args),
            "code": str(completed.returncode),
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
    )
    return completed.returncode == 0


def command_error_message(default_message):
    combined = "\n".join(
        item
        for item in [LAST_COMMAND.get("stdout", ""), LAST_COMMAND.get("stderr", "")]
        if item
    )
    if combined:
        return extract_error_summary(combined, LAST_COMMAND.get("code"))
    return default_message


def update_run_state(**changes):
    with RUN_LOCK:
        RUN_STATE.update(changes)


def get_run_state():
    with RUN_LOCK:
        return dict(RUN_STATE)


def get_live_run_state():
    with RUN_LOCK:
        now = time.time()
        if RUN_STATE.get("running") and not RUN_STATE.get("paused"):
            stage = RUN_STATE.get("stage", "Starting")
            stage_started = RUN_STATE.get("stage_started_ts") or RUN_STATE.get("started_ts") or now
            expected_seconds = AGENT_EXPECTED_SECONDS.get(stage, 30)
            elapsed_in_stage = max(0, now - stage_started)
            step_percent = expected_time_progress(elapsed_in_stage, expected_seconds)
            agent_progress = dict(RUN_STATE.get("agent_progress") or {})
            if stage in AGENT_ORDER:
                agent_progress[stage] = max(int(agent_progress.get(stage, 0)), step_percent)
            item_total_percent = itemwise_total_progress(
                RUN_STATE.get("item_completed", 0),
                RUN_STATE.get("item_total", 0),
                stage,
                step_percent,
            )
            if item_total_percent is None:
                if stage == "Reader":
                    total_percent = 0
                else:
                    elapsed_total = max(0, now - (RUN_STATE.get("started_ts") or now))
                    total_percent = expected_time_progress(elapsed_total, TOTAL_EXPECTED_SECONDS)
            else:
                total_percent = item_total_percent
            RUN_STATE["agent_progress"] = agent_progress
            RUN_STATE["step_progress"] = step_percent if stage in AGENT_ORDER else 0
            RUN_STATE["progress"] = max(int(RUN_STATE.get("progress") or 0), total_percent)
            RUN_STATE["last_progress_at"] = now

            heartbeat_messages = STAGE_HEARTBEATS.get(stage)
            last_heartbeat = RUN_STATE.get("last_heartbeat_at") or 0
            if heartbeat_messages and now - last_heartbeat >= 20:
                index = int(elapsed_in_stage // 20) % len(heartbeat_messages)
                timestamp = time.strftime("%H:%M:%S")
                current = RUN_STATE.get("log", "")
                line = f"[{timestamp}] {heartbeat_messages[index]}\n"
                RUN_STATE["log"] = (current + line)[-30000:]
                RUN_STATE["last_heartbeat_at"] = now
        state = dict(RUN_STATE)
        started_ts = state.get("started_ts") or 0
        finished_ts = state.get("finished_ts") or 0
        if started_ts:
            end_ts = now if state.get("running") else finished_ts or now
            state["elapsed_seconds"] = max(0, int(end_ts - started_ts))
            state["elapsed"] = format_elapsed(state["elapsed_seconds"])
        else:
            state["elapsed_seconds"] = 0
            state["elapsed"] = "-"
        try:
            options = load_options()
            output_dir = project_path(options.get("output_dir", "outputs"))
            questions_path = output_dir / "generated_questions_final.md"
            answers_path = output_dir / "answer_key_final.md"
            state["live_outputs"] = {
                "questions": read_text(questions_path) if questions_path.exists() else "",
                "answers": read_text(answers_path) if answers_path.exists() else "",
                "questions_mtime": int(questions_path.stat().st_mtime) if questions_path.exists() else 0,
                "answers_mtime": int(answers_path.stat().st_mtime) if answers_path.exists() else 0,
            }
        except Exception:
            state["live_outputs"] = {"questions": "", "answers": "", "questions_mtime": 0, "answers_mtime": 0}
        state["code_signature"] = code_signature()
        state["code_time_machine"] = code_time_machine_payload()
        return state


def format_elapsed(seconds):
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:d}:{secs:02d}"


def append_run_log(line):
    with RUN_LOCK:
        current = RUN_STATE.get("log", "")
        RUN_STATE["log"] = (current + line)[-30000:]


def extract_error_summary(log_text, returncode):
    lines = [line.strip() for line in (log_text or "").splitlines() if line.strip()]
    joined = "\n".join(lines)
    if "getaddrinfo failed" in joined or "urlopen error" in joined:
        return (
            "DeepSeek API request failed because the network/DNS lookup failed.\n"
            "Check internet/proxy/firewall settings and verify DEEPSEEK_API_BASE in .env.\n"
            "Full traceback is preserved in the command log below."
        )
    if "WinError 10054" in joined or "forcibly closed by the remote host" in joined:
        return (
            "DeepSeek API request was interrupted: the remote host forcibly closed the connection.\n"
            "This is usually a transient network/proxy/API gateway disconnect, not a UI formatting error.\n"
            "Retry the run, and if it repeats check proxy/firewall/VPN stability and DeepSeek API availability.\n"
            "Full traceback is preserved in the command log below."
        )
    important = [
        line
        for line in lines
        if (
            "error" in line.lower()
            or "failed" in line.lower()
            or "traceback" in line.lower()
            or "exception" in line.lower()
            or "failure status" in line.lower()
        )
    ]
    tail = important[-6:] if important else lines[-6:]
    summary = "\n".join(tail).strip()
    if not summary:
        summary = f"Pipeline stopped with return code {returncode}."
    return clean_error_summary(summary, returncode)


def clean_error_summary(text, returncode=None):
    raw = display_text(text)
    if "MissingDateHeader" in raw or "x-bce-date" in raw:
        return (
            "PaddleOCR result download was rejected by the BOS result URL because API auth headers were attached. "
            "The UI now fetches the signed result URL without Authorization headers; retry OCR. "
            "If it still fails, check whether the PaddleOCR result link expired or the token changed."
        )
    lines = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        lowered = stripped.lower()
        if (
            lowered.startswith("traceback")
            or lowered.startswith("raise ")
            or lowered.startswith("the above exception")
            or " from error" in lowered
        ):
            continue
        lines.append(stripped)
    summary = "\n".join(lines[-5:]).strip()
    if not summary:
        summary = f"Pipeline stopped with return code {returncode}."
    return summary


def infer_progress(line):
    lowered = line.lower()
    if "pipeline paused before" in lowered:
        update_run_state(
            paused=True,
            stage="Paused",
            paused_at=time.strftime("%H:%M:%S"),
            last_progress_at=time.time(),
        )
        return
    if "pipeline resumed" in lowered:
        update_run_state(
            paused=False,
            paused_at="",
            stage="Running",
            last_progress_at=time.time(),
            stage_started_ts=time.time(),
        )
        return
    stage = None
    item_match = re.search(
        r"===\s*Reader\s+item\s+(\d+)\s*/\s*(\d+):\s+running\s+full\s+pipeline\s*===",
        line,
        re.IGNORECASE,
    )
    professional_item_match = re.search(
        r"===\s*Professional\s+item\s+(\d+)\s*/\s*(\d+):\s+candidate\s+(\d+)\s*/\s*(\d+)\s*===",
        line,
        re.IGNORECASE,
    )
    if item_match or professional_item_match:
        match = item_match or professional_item_match
        item_index = int(match.group(1))
        item_total = int(match.group(2))
        agent_progress = {agent: 0 for agent in AGENT_ORDER}
        agent_progress["Reader"] = 100
        update_run_state(
            stage="Reader item" if item_match else "Professional item",
            item_index=item_index,
            item_total=item_total,
            item_completed=max(0, item_index - 1),
            agent_progress=agent_progress,
            last_progress_at=time.time(),
            stage_started_ts=time.time(),
            last_heartbeat_at=0,
            step_progress=0,
        )
        return

    running_match = re.search(
        r"===\s*Round\s+(\d+):\s+running\s+(Reader|Decomposer|Reviewer|Generator|QC)\s*===",
        line,
        re.IGNORECASE,
    )
    if running_match:
        round_value = running_match.group(1)
        stage = AGENT_NAME_MAP.get(running_match.group(2).lower(), running_match.group(2))
    elif re.search(r"revising\s+Decomposer", line, re.IGNORECASE):
        stage = "Decomposer"
    elif re.search(r"revising\s+Generator", line, re.IGNORECASE):
        stage = "Generator"

    if not running_match:
        round_value = None
    marker = "Round "
    if not running_match and marker in line:
        after = line.split(marker, 1)[1]
        round_value = after.split(":", 1)[0].strip()

    changes = {}
    if stage:
        previous_stage = get_run_state().get("stage")
        changes["stage"] = stage
        changes["last_progress_at"] = time.time()
        if previous_stage != stage:
            current_state = get_run_state()
            agent_progress = dict(current_state.get("agent_progress") or {})
            if previous_stage in AGENT_ORDER:
                agent_progress[previous_stage] = 100
            if stage in AGENT_ORDER:
                agent_progress[stage] = 0
            if stage in ITEM_STAGE_ORDER and int(current_state.get("item_total") or 0) > 0:
                changes["item_completed"] = max(0, int(current_state.get("item_index") or 1) - 1)
            changes["agent_progress"] = agent_progress
            changes["stage_started_ts"] = time.time()
            changes["last_heartbeat_at"] = 0
            changes["step_progress"] = 0
    if round_value:
        changes["round"] = round_value
    if changes:
        update_run_state(**changes)


def run_background(args):
    global CURRENT_PROCESS
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    started_ts = get_run_state().get("started_ts") or time.time()
    LAST_COMMAND.update({"cmd": " ".join(args), "code": "running", "stdout": "", "stderr": ""})
    append_run_log(f"UI runner command: {' '.join(args)}\n")
    append_run_log(f"UI runner cwd: {PROJECT_ROOT}\n")
    append_run_log(f"UI server python: {sys.executable}\n")

    try:
        process = subprocess.Popen(
            args,
            cwd=PROJECT_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
        )
        with PROCESS_LOCK:
            CURRENT_PROCESS = process
        update_run_state(pid=str(process.pid))
        for line in process.stdout or []:
            append_run_log(line)
            infer_progress(line)
        returncode = process.wait()
        final_log = get_run_state().get("log", "")
        LAST_COMMAND.update(
            {
                "cmd": " ".join(args),
                "code": str(returncode),
                "stdout": final_log,
                "stderr": "",
            }
        )
        options = load_options()
        failure_path = project_path(options.get("output_dir", "outputs")) / "pipeline_failed.json"
        recent_failure_file = False
        try:
            recent_failure_file = failure_path.exists() and failure_path.stat().st_mtime >= started_ts - 1
        except OSError:
            recent_failure_file = False
        pipeline_reported_failure = recent_failure_file or "Failure status:" in final_log
        if returncode == 0 and not pipeline_reported_failure:
            update_run_state(
                running=False,
                stage="Complete",
                progress=100,
                step_progress=100,
                agent_progress={agent: 100 for agent in AGENT_ORDER},
                item_completed=get_run_state().get("item_total", 0),
                returncode=str(returncode),
                finished_at=time.strftime("%H:%M:%S"),
                finished_ts=time.time(),
            )
        else:
            failure = read_json(failure_path, {}) if recent_failure_file else {}
            if get_run_state().get("stop_requested") or returncode in {-1, 4294967295}:
                error_text = "Pipeline was stopped by the user."
                failed_stage = "Stopped"
            else:
                error_text = failure.get("error") or failure.get("reason") or extract_error_summary(final_log, returncode)
                failed_stage = "Failed"
            live_state = get_live_run_state()
            update_run_state(
                running=False,
                stage=failed_stage,
                progress=live_state.get("progress", 0),
                step_progress=live_state.get("step_progress", 0),
                returncode=str(returncode),
                error_message=error_text,
                finished_at=time.strftime("%H:%M:%S"),
                finished_ts=time.time(),
            )
    except Exception as exc:
        append_run_log(f"\nUI runner error: {exc}\n")
        error_text = f"UI runner error: {exc}"
        LAST_COMMAND.update(
            {
                "cmd": " ".join(args),
                "code": "error",
                "stdout": get_run_state().get("log", ""),
                "stderr": str(exc),
            }
        )
        live_state = get_live_run_state()
        update_run_state(
            running=False,
            stage="Failed",
            progress=live_state.get("progress", 0),
            step_progress=live_state.get("step_progress", 0),
            returncode="error",
            error_message=error_text,
            finished_at=time.strftime("%H:%M:%S"),
            finished_ts=time.time(),
        )
    finally:
        with PROCESS_LOCK:
            CURRENT_PROCESS = None


def start_background_command(args):
    if get_run_state().get("running"):
        return False
    try:
        path = pause_flag_path()
        if path.exists():
            path.unlink()
    except OSError:
        pass
    now = time.time()
    update_run_state(
        running=True,
        stage="Starting",
        round="-",
        progress=0,
        step_progress=0,
        agent_progress={agent: 0 for agent in AGENT_ORDER},
        last_progress_at=now,
        stage_started_ts=now,
        last_heartbeat_at=0,
        started_at=time.strftime("%H:%M:%S"),
        started_ts=now,
        finished_at="",
        finished_ts=0,
        returncode="",
        cmd=" ".join(args),
        log="",
        error_message="",
        item_index=0,
        item_total=0,
        item_completed=0,
        pid="",
        stop_requested=False,
        paused=False,
        paused_at="",
        pause_owner="",
    )
    thread = threading.Thread(target=run_background, args=(args,), daemon=True)
    thread.start()
    return True


def stop_background_command():
    with PROCESS_LOCK:
        process = CURRENT_PROCESS
    if not process or process.poll() is not None:
        return False
    try:
        update_run_state(stop_requested=True)
        try:
            path = pause_flag_path()
            if path.exists():
                path.unlink()
        except OSError:
            pass
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        append_run_log("\nUI stopped the running pipeline.\n")
        live_state = get_live_run_state()
        update_run_state(
            running=False,
            stage="Stopped",
            progress=live_state.get("progress", 0),
            step_progress=live_state.get("step_progress", 0),
            returncode="stopped",
            error_message="Pipeline was stopped by the user.",
            stop_requested=True,
            paused=False,
            paused_at="",
            pause_owner="",
            finished_at=time.strftime("%H:%M:%S"),
            finished_ts=time.time(),
        )
        return True
    except Exception as exc:
        update_run_state(error_message=f"Stop failed: {exc}")
        return False


def pause_flag_path():
    options = load_options()
    return project_path(options.get("output_dir", "outputs")) / PAUSE_FILENAME


def pause_background_command(owner=""):
    if not get_run_state().get("running"):
        return False
    path = pause_flag_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"paused_at={time.strftime('%Y-%m-%d %H:%M:%S')}\nowner={owner or 'current step'}\n",
        encoding="utf-8",
    )
    append_run_log(f"\nUI requested pause at {owner or 'current step'}. Pipeline will pause before the next safe step.\n")
    update_run_state(
        paused=True,
        paused_at=time.strftime("%H:%M:%S"),
        pause_owner=owner or "ui",
        stage="Paused",
        last_progress_at=time.time(),
    )
    return True


def resume_background_command():
    path = pause_flag_path()
    existed = path.exists()
    try:
        if existed:
            path.unlink()
    except OSError as exc:
        update_run_state(error_message=f"Resume failed: {exc}")
        return False
    append_run_log("\nUI resumed the pipeline.\n")
    update_run_state(
        paused=False,
        paused_at="",
        pause_owner="",
        last_progress_at=time.time(),
        stage="Running" if get_run_state().get("running") else get_run_state().get("stage", "Idle"),
    )
    return existed or get_run_state().get("running")


def join_preview_lines(left, right):
    if not left:
        return right
    if not right:
        return left
    if re.search(r"[\u4e00-\u9fff]$", left) and re.match(r"^[\u4e00-\u9fff，。；：！？、）】》]", right):
        return left + right
    if re.match(r"^[,.;:!?)]", right):
        return left + right
    return left + " " + right


def normalize_markdown_preview_text(text):
    lines = display_text(text).replace("\r\n", "\n").replace("\r", "\n").splitlines()
    merged = []
    paragraph = ""
    in_fence = False
    in_math_block = False

    def flush_paragraph():
        nonlocal paragraph
        if paragraph:
            merged.append(paragraph)
            paragraph = ""

    for raw in lines:
        stripped = raw.strip()
        structural = (
            not stripped
            or stripped.startswith(("# ", "## ", "### ", "- ", "* ", "> ", "|", "```"))
            or re.match(r"^\d+[.)]\s+", stripped)
            or stripped in {"$$", "\\[", "\\]"}
            or stripped.startswith("$$")
            or stripped.endswith("$$")
        )
        if stripped.startswith("```"):
            flush_paragraph()
            merged.append(raw)
            in_fence = not in_fence
            continue
        if in_fence:
            merged.append(raw)
            continue
        if stripped in {"$$", "\\["}:
            flush_paragraph()
            merged.append(raw)
            in_math_block = True
            continue
        if stripped in {"$$", "\\]"} and in_math_block:
            merged.append(raw)
            in_math_block = False
            continue
        if in_math_block:
            merged.append(raw)
            continue
        if structural:
            flush_paragraph()
            merged.append(raw)
        else:
            paragraph = join_preview_lines(paragraph, stripped)
    flush_paragraph()
    return "\n".join(merged)


def md_to_html(text):
    out = []
    in_list = False
    in_math_block = False
    math_lines = []
    preview_text = normalize_markdown_preview_text(sanitize_math_markdown(text)) or "No content yet."
    for raw in html.escape(preview_text).splitlines():
        line = raw.strip()
        if line in {"$$", "\\["} and not in_math_block:
            if in_list:
                out.append("</ul>")
                in_list = False
            in_math_block = True
            math_lines = [line]
        elif in_math_block:
            math_lines.append(raw)
            if line in {"$$", "\\]"}:
                out.append('<div class="math-block">' + "\n".join(math_lines) + "</div>")
                in_math_block = False
                math_lines = []
        elif line.startswith("### "):
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f"<h3>{line[4:]}</h3>")
        elif line.startswith("## "):
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f"<h2>{line[3:]}</h2>")
        elif line.startswith("# "):
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f"<h1>{line[2:]}</h1>")
        elif line.startswith("- "):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{line[2:]}</li>")
        elif not line:
            if in_list:
                out.append("</ul>")
                in_list = False
            continue
        else:
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f"<p>{raw}</p>")
    if in_list:
        out.append("</ul>")
    if in_math_block and math_lines:
        out.append('<div class="math-block">' + "\n".join(math_lines) + "</div>")
    return "\n".join(out).replace("**", "")


def esc(value):
    return html.escape(str(value), quote=True)


def checked(value):
    return "checked" if value else ""


def selected(value, target):
    return "selected" if value == target else ""


def status(options):
    output_dir = project_path(options["output_dir"])
    qc = read_json(output_dir / "qc_final.json")
    failure = read_json(output_dir / "pipeline_failed.json")
    coverage = read_json(PROJECT_ROOT / "docs" / "input_coverage_report.json", {})
    if qc:
        state = "Passed" if qc.get("is_passed") else "Needs review"
        score = qc.get("score", "-")
        note = qc.get("qc_summary", "")
    elif failure:
        state, score, note = "Failed", "-", failure.get("error") or failure.get("reason") or ""
    else:
        state, score, note = "Idle", "-", "No final output found."
    return state, score, coverage.get("question_count", "-"), note


def replay_payload(options):
    return scan_time_machine(
        PROJECT_ROOT,
        output_dirs=[options.get("output_dir", "outputs"), "outputs", "outputs_professional"],
        result_root=options.get("result_store", {}).get("dir", "result"),
    )


def repair_question_bank_text(value):
    if isinstance(value, list):
        return [repair_question_bank_text(item) for item in value]
    if isinstance(value, dict):
        return {key: repair_question_bank_text(item) for key, item in value.items()}
    if not isinstance(value, str):
        return value
    if not any(marker in value for marker in ["鎴", "鐨", "涓", "鍙", "鏄", "绋", "€"]):
        return value
    try:
        fixed = value.encode("gb18030", errors="strict").decode("utf-8", errors="strict")
        if sum("\u4e00" <= char <= "\u9fff" for char in fixed) >= sum("\u4e00" <= char <= "\u9fff" for char in value):
            return fixed
    except UnicodeError:
        pass
    return value


def question_bank_paths(options):
    result_dir = options.get("result_store", {}).get("dir", "result")
    root = project_path(result_dir) / "question_bank"
    return root, root / "questions.jsonl", root / "index.json"


def normalize_bank_query(text):
    return re.sub(r"\s+", "", str(text or "")).lower()


def compact_bank_record(record):
    tags = record.get("tag_names") or []
    if not tags and isinstance(record.get("tags"), dict):
        for values in record.get("tags", {}).values():
            for item in values or []:
                if isinstance(item, dict) and (item.get("name") or item.get("tag_id")):
                    tags.append(item.get("name") or item.get("tag_id"))
    return {
        "id": repair_question_bank_text(record.get("item_id") or record.get("marwyn_id") or ""),
        "bank_id": repair_question_bank_text(record.get("bank_id", "")),
        "grade": repair_question_bank_text(record.get("grade_band") or ""),
        "difficulty": repair_question_bank_text(record.get("difficulty") or ""),
        "type": repair_question_bank_text(record.get("question_type_field") or ""),
        "stem": repair_question_bank_text(record.get("stem") or record.get("prompt") or ""),
        "answer": repair_question_bank_text(record.get("answer") or ""),
        "tags": repair_question_bank_text(tags[:10]),
    }


def question_bank_summary_payload(options):
    root, questions_path, index_path = question_bank_paths(options)
    index = repair_question_bank_text(read_json(index_path, {}))
    top_tags = [
        {"name": str(item[0]), "count": item[1]}
        for item in (index.get("top_tags") or [])
        if isinstance(item, list) and item
    ]
    return {
        "ok": questions_path.exists(),
        "message": "Question bank ready." if questions_path.exists() else f"Question bank not found: {rel_path(questions_path)}",
        "root": str(root),
        "index": index,
        "top_tags": repair_question_bank_text(top_tags[:80]),
        "records": [],
        "lazy": True,
    }


def load_question_bank_records(options, query="", tag="", limit=40):
    root, questions_path, index_path = question_bank_paths(options)
    summary = question_bank_summary_payload(options)
    index = summary.get("index", {})
    if not questions_path.exists():
        return {
            "ok": False,
            "message": f"Question bank not found: {rel_path(questions_path)}",
            "index": index,
            "top_tags": summary.get("top_tags", []),
            "records": [],
        }
    query_norm = normalize_bank_query(repair_question_bank_text(query))
    tag_norm = normalize_bank_query(repair_question_bank_text(tag))
    records = []
    with questions_path.open("r", encoding="utf-8-sig") as file:
        for line in file:
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            item = compact_bank_record(raw)
            haystack = normalize_bank_query(
                "\n".join(
                    [
                        item.get("stem", ""),
                        item.get("answer", ""),
                        item.get("type", ""),
                        item.get("grade", ""),
                        " ".join(item.get("tags", [])),
                    ]
                )
            )
            if query_norm and query_norm not in haystack:
                continue
            if tag_norm and tag_norm not in normalize_bank_query(" ".join(item.get("tags", []))):
                continue
            records.append(item)
            if len(records) >= max(1, min(int(limit or 40), 120)):
                break
    return {
        "ok": True,
        "message": "Question bank loaded.",
        "root": str(root),
        "index": index,
        "top_tags": summary.get("top_tags", []),
        "records": records,
    }


def render_page(message=""):
    options = load_options()
    profile = options["generation_profile"]
    models = options["models"]
    thinking = options["thinking"]
    effort = options["reasoning_effort"]
    budget = options["token_budget"]
    runtime = options["runtime"]
    result_store = options["result_store"]
    input_path = project_path(options["input"])
    input_text = read_text(input_path)
    output_dir = project_path(options["output_dir"])
    coverage = read_json(PROJECT_ROOT / "docs" / "input_coverage_report.json", {})
    questions_path = output_dir / "generated_questions_final.md"
    answers_path = output_dir / "answer_key_final.md"
    bank_similar_path = output_dir / "question_bank_similar.md"
    preview_path = output_dir / "preview.md"
    qc_path = output_dir / "qc_final.json"
    questions_text = read_fresh_output(input_path, questions_path)
    answers_text = read_fresh_output(input_path, answers_path)
    bank_similar_text = read_fresh_output(input_path, bank_similar_path)
    preview_text = read_fresh_output(input_path, preview_path)
    qc = read_json(qc_path) if is_fresh_output(input_path, qc_path) else None
    failure = read_json(output_dir / "pipeline_failed.json")
    replay = {"runs": [], "lazy": True}
    state, qc_score, question_count, note = status(options)
    run_state = get_run_state()
    debug_dir = output_dir / "debug"
    debug_files = sorted(debug_dir.glob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True) if debug_dir.exists() else []
    debug_text = read_text(debug_files[0], "No debug files found.") if debug_files else "No debug files found."
    current_code_signature = code_signature()

    model_inputs = "\n".join(
        f'<label>{name} model<input name="model_{name}" value="{esc(models.get(name, ""))}"></label>'
        for name in ["default", "reader", "decomposer", "reviewer", "generator", "qc"]
    )
    agent_inputs = "\n".join(
        f"""
        <div class="agent-row">
          <label><input type="checkbox" name="thinking_{name}" {checked(thinking.get(name))}> {name} thinking</label>
          <select name="effort_{name}">
            <option {selected(effort.get(name), "low")}>low</option>
            <option {selected(effort.get(name), "medium")}>medium</option>
            <option {selected(effort.get(name), "high")}>high</option>
          </select>
        </div>
        """
        for name in ["reader", "decomposer", "reviewer", "generator", "qc"]
    )
    budget_inputs = "\n".join(
        f'<label>{key}<input type="number" name="budget_{key}" value="{esc(budget.get(key, 0))}" step="1000"></label>'
        for key in ["total_warning", "total_stop", "single_warning", "single_stop"]
    )
    runtime_inputs = "\n".join(
        [
            f'<label>request timeout seconds<input type="number" name="runtime_request_timeout_seconds" value="{esc(runtime.get("request_timeout_seconds", 90))}" min="1" max="3600"></label>',
            f'<label>generator warning seconds<input type="number" name="runtime_generator_warning_seconds" value="{esc(runtime.get("generator_warning_seconds", 120))}" min="0" max="3600"></label>',
            f'<label>generator timeout seconds<input type="number" name="runtime_generator_timeout_seconds" value="{esc(runtime.get("generator_timeout_seconds", 180))}" min="1" max="3600"></label>',
            f'<label><input type="checkbox" name="runtime_fallback_on_error" {checked(runtime.get("fallback_on_error"))}> fallback on error</label>',
            f'<label><input type="checkbox" name="runtime_fallback_on_timeout" {checked(runtime.get("fallback_on_timeout"))}> fallback on timeout</label>',
        ]
    )
    result_store_inputs = "\n".join(
        [
            f'<label><input type="checkbox" name="result_store_enabled" {checked(result_store.get("enabled"))}> save and use result library</label>',
            f'<label>result dir<input name="result_store_dir" value="{esc(result_store.get("dir", "result"))}"></label>',
        ]
    )

    qc_html = f"<pre>{esc(json.dumps(qc, ensure_ascii=False, indent=2))}</pre>" if qc else "<p>No QC result yet.</p>"
    if failure and not qc:
        qc_html = f'<p class="danger">{esc(failure.get("error") or failure.get("reason") or "Pipeline failed.")}</p><pre>{esc(json.dumps(failure, ensure_ascii=False, indent=2))}</pre>'

    coverage_cards = ""
    for key, label in [("line_coverage", "Line"), ("condition_coverage", "Condition"), ("branch_coverage", "Branch")]:
        data = coverage.get(key, {})
        coverage_cards += f'<div class="metric"><span>{label}</span><strong>{esc(data.get("covered", "-"))}/{esc(data.get("total", "-"))}</strong><em>{float(data.get("ratio", 0))*100:.1f}%</em></div>'
    input_text_json = json.dumps(input_text, ensure_ascii=False)
    code_signature_json = json.dumps(current_code_signature, ensure_ascii=False)
    replay_json = json.dumps(replay, ensure_ascii=False)
    code_time_machine_json = json.dumps(code_time_machine_payload(), ensure_ascii=False)
    bank_payload = question_bank_summary_payload(options)
    bank_payload_json = json.dumps(bank_payload, ensure_ascii=False)
    agent_progress = run_state.get("agent_progress") or {}
    agent_branch_html = "\n".join(
        [
            (
                f'<div class="agent-branch" data-agent="{agent}">'
                f'<div class="agent-branch-title"><span>{agent}</span><strong id="agent-{agent.lower()}-text">{esc(agent_progress.get(agent, 0))}%</strong></div>'
                f'<div class="step-track"><div id="agent-{agent.lower()}-fill" class="step-fill" style="width: {esc(agent_progress.get(agent, 0))}%"></div></div>'
                f'</div>'
            )
            for agent in AGENT_ORDER
        ]
    )
    replay_options = '<option value="">点击“载入”读取运行回放</option>'

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Math Quiz Workbench</title>
  <script>
    window.MathJax = {{tex: {{inlineMath: [['$', '$'], ['\\\\(', '\\\\)']], displayMath: [['$$', '$$'], ['\\\\[', '\\\\]']]}}}};
  </script>
  <script defer src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-svg.js"></script>
  <style>
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: "Segoe UI", Arial, sans-serif; color: #182230; background: #f6f8fb; }}
    header {{ padding: 18px 24px 10px; border-bottom: 1px solid #d8dde8; background: #fff; }}
    .header-row {{ display: flex; justify-content: space-between; gap: 18px; align-items: flex-start; }}
    .mode-controls {{ display: grid; gap: 10px; min-width: 300px; justify-items: end; }}
    .mode-group {{ display: flex; align-items: center; gap: 8px; color: #667085; font-size: 12px; }}
    .segmented {{ display: inline-flex; border: 1px solid #cdd5df; border-radius: 8px; overflow: hidden; background: #fff; }}
    .segmented button {{ border: 0; border-radius: 0; background: #fff; color: #344054; padding: 7px 10px; }}
    .segmented button.active {{ background: #0f766e; color: #fff; }}
    h1 {{ margin: 0; font-size: 26px; }}
    h2 {{ margin: 0 0 12px; font-size: 18px; }}
    h3 {{ margin: 14px 0 8px; font-size: 15px; }}
    .caption {{ margin-top: 4px; color: #667085; }}
    .wrap {{ display: grid; grid-template-columns: 360px minmax(0, 1fr); min-height: calc(100vh - 78px); }}
    .wrap.user-mode {{ grid-template-columns: minmax(0, 1fr); }}
    .mode-hidden {{ display: none !important; }}
    aside {{ padding: 16px; background: #fff; border-right: 1px solid #d8dde8; overflow: auto; }}
    main {{ padding: 16px 20px 28px; overflow: auto; }}
    .status {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; margin-bottom: 14px; }}
    .cell, .panel, .metric {{ border: 1px solid #d8dde8; background: #fff; border-radius: 8px; padding: 12px; }}
    .cell span, .metric span {{ display: block; color: #667085; font-size: 12px; margin-bottom: 5px; }}
    .cell strong, .metric strong {{ display: block; overflow-wrap: anywhere; }}
    .metric em {{ color: #0f766e; font-style: normal; }}
    .grid {{ display: grid; grid-template-columns: .9fr 1.25fr; gap: 16px; align-items: start; }}
    label {{ display: block; font-size: 13px; color: #344054; margin: 9px 0; }}
    input, textarea, select {{ width: 100%; margin-top: 4px; border: 1px solid #cdd5df; border-radius: 6px; padding: 8px; font: inherit; background: #fff; }}
    input[type="checkbox"] {{ width: auto; margin-right: 6px; }}
    textarea {{ min-height: 160px; resize: vertical; }}
    .input-editor {{ min-height: 460px; font-family: Consolas, monospace; }}
    .input-viewbar {{ display: flex; gap: 8px; margin-bottom: 8px; }}
    .input-viewbar button {{ padding: 7px 10px; }}
    .input-viewbar button.active {{ background: #eefaf7; color: #0f766e; }}
    .ocr-panel {{ border: 1px solid #d8dde8; border-radius: 8px; background: #f8fafc; margin-bottom: 10px; overflow: hidden; }}
    .ocr-panel summary {{ cursor: pointer; list-style: none; display: flex; justify-content: space-between; gap: 12px; align-items: center; padding: 10px 12px; color: #182230; font-weight: 700; }}
    .ocr-panel summary::-webkit-details-marker {{ display: none; }}
    .ocr-panel summary span {{ color: #667085; font-size: 12px; font-weight: 500; }}
    .ocr-body {{ border-top: 1px solid #d8dde8; padding: 12px; }}
    .ocr-upload {{ display: grid; grid-template-columns: 1.2fr .8fr .8fr; gap: 10px; align-items: end; }}
    .ocr-upload .ocr-file {{ grid-column: 1 / -1; }}
    .ocr-upload input[type="file"] {{ padding: 8px; min-height: 40px; }}
    .ocr-dropzone {{ grid-column: 1 / -1; border: 1px dashed #98a2b3; background: #fff; border-radius: 8px; padding: 16px; text-align: center; color: #667085; cursor: pointer; transition: border-color .15s ease, background .15s ease; }}
    .ocr-dropzone strong {{ display: block; color: #182230; margin-bottom: 4px; }}
    .ocr-dropzone.dragging {{ border-color: #0f766e; background: #eefaf7; color: #0f766e; }}
    .ocr-actions {{ display: flex; gap: 10px; align-items: center; justify-content: space-between; margin-top: 10px; }}
    .ocr-actions button {{ min-width: 120px; }}
    .ocr-status {{ color: #667085; font-size: 13px; min-height: 18px; flex: 1; line-height: 1.35; }}
    .ocr-progress {{ display: none; margin-top: 10px; }}
    .ocr-progress.visible {{ display: block; }}
    .ocr-progress-head {{ display: flex; justify-content: space-between; gap: 10px; color: #344054; font-size: 13px; margin-bottom: 5px; }}
    .ocr-progress-track {{ height: 8px; border-radius: 999px; background: #e4e7ec; overflow: hidden; }}
    .ocr-progress-fill {{ height: 100%; width: 0%; background: #2563eb; transition: width .35s ease; }}
    .ocr-progress-fill.running {{
      background-image: linear-gradient(45deg, rgba(255,255,255,.28) 25%, transparent 25%, transparent 50%, rgba(255,255,255,.28) 50%, rgba(255,255,255,.28) 75%, transparent 75%, transparent);
      background-size: 18px 18px;
      animation: progress-stripes .8s linear infinite;
    }}
    .ocr-result {{ display: none; border: 1px solid #d8dde8; border-radius: 8px; background: #fff; margin-top: 10px; padding: 10px; }}
    .ocr-result.visible {{ display: block; }}
    .ocr-result-head {{ display: flex; justify-content: space-between; gap: 10px; align-items: center; margin-bottom: 8px; }}
    .ocr-result-head strong {{ color: #182230; }}
    .ocr-result-meta {{ display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 8px; }}
    .ocr-pill {{ border: 1px solid #d8dde8; background: #f8fafc; color: #344054; border-radius: 999px; padding: 3px 8px; font-size: 12px; }}
    .ocr-warning {{ border-color: #fedf89; background: #fffaeb; color: #93370d; }}
    .ocr-result-preview {{ max-height: 560px; overflow: auto; padding: 0; }}
    .ocr-result-preview img {{ width: 100%; max-height: 540px; object-fit: contain; display: block; background: #fff; }}
    .ocr-result-preview iframe {{ width: 100%; height: 540px; border: 0; display: block; background: #fff; }}
    .ocr-result-actions {{ display: flex; gap: 8px; flex-wrap: wrap; margin-top: 8px; }}
    .ocr-source-view {{ display: none; margin-top: 10px; border: 1px solid #d8dde8; border-radius: 8px; overflow: hidden; background: #f8fafc; }}
    .ocr-source-view.visible {{ display: block; }}
    .ocr-source-view iframe, .ocr-source-view img {{ width: 100%; max-height: 520px; border: 0; display: block; object-fit: contain; background: #fff; }}
    .ocr-text-details {{ margin-top: 10px; border: 1px solid #d8dde8; border-radius: 8px; background: #fff; overflow: hidden; }}
    .ocr-text-details summary {{ cursor: pointer; padding: 8px 10px; color: #344054; font-weight: 700; }}
    .ocr-text-preview {{ border: 0; border-top: 1px solid #d8dde8; border-radius: 0; max-height: 260px; }}
    .ocr-question-picker {{ display: none; margin-top: 10px; border: 1px solid #d8dde8; border-radius: 8px; background: #f8fafc; padding: 10px; }}
    .ocr-question-picker.visible {{ display: block; }}
    .ocr-question-head {{ display: flex; justify-content: space-between; align-items: center; gap: 10px; color: #344054; font-weight: 700; }}
    .ocr-question-tools {{ display: flex; gap: 6px; flex-wrap: wrap; }}
    .ocr-question-tools button {{ min-height: 28px; padding: 4px 8px; font-size: 12px; }}
    .ocr-question-list {{ display: grid; gap: 8px; max-height: 240px; overflow: auto; margin-top: 8px; }}
    .ocr-question-option {{ display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 8px; align-items: start; border: 1px solid #e4e7ec; background: #fff; border-radius: 6px; padding: 9px; color: #344054; }}
    .ocr-question-option span {{ overflow-wrap: anywhere; }}
    .ocr-question-option small {{ display: block; color: #667085; margin-top: 3px; line-height: 1.35; }}
    .input-preview {{ min-height: 460px; max-height: 640px; overflow: auto; }}
    .input-preview.placeholder {{ color: #98a2b3; }}
    .hidden {{ display: none !important; }}
    button {{ border: 1px solid #0f766e; background: #0f766e; color: white; border-radius: 6px; padding: 9px 11px; font-weight: 700; cursor: pointer; }}
    button.secondary {{ background: #fff; color: #0f766e; }}
    button.danger-button {{ border-color: #fda29b; color: #b42318; background: #fff; }}
    button.danger-button:hover {{ background: #fff3f0; }}
    .actions {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin: 10px 0 16px; }}
    .tabs {{ display: flex; gap: 6px; border-bottom: 1px solid #d8dde8; margin-bottom: 12px; flex-wrap: wrap; }}
    .tab-button {{ background: #fff; color: #344054; border: 1px solid #d8dde8; border-bottom: 0; padding: 8px 10px; }}
    .tab-button.active {{ color: #0f766e; background: #eefaf7; }}
    .tab {{ display: none; }}
    .tab.active {{ display: block; }}
    .preview {{ background: #fff; border: 1px solid #d8dde8; border-radius: 8px; padding: 10px; line-height: 1.22; overflow-x: auto; }}
    .preview p {{ margin: 0 0 16px; }}
    .preview .math-block {{ margin: 10px 0 18px; overflow-x: auto; white-space: pre-wrap; }}
    .preview h1, .preview h2, .preview h3 {{ margin: 10px 0 6px; line-height: 1.18; }}
    .preview ul, .preview ol {{ margin: 6px 0 14px 22px; padding: 0; }}
    .preview li {{ margin: 0 0 6px; }}
    .preview br {{ display: none; }}
    .bank-toolbar {{ display: grid; grid-template-columns: minmax(220px, 1fr) minmax(180px, 260px) auto; gap: 8px; align-items: end; margin-bottom: 12px; }}
    .bank-summary {{ display: flex; flex-wrap: wrap; gap: 8px; color: #667085; font-size: 12px; margin: 0 0 10px; }}
    .bank-summary span {{ border: 1px solid #d8dde8; border-radius: 999px; padding: 4px 8px; background: #f8fafc; }}
    .bank-tag-cloud {{ display: flex; flex-wrap: wrap; gap: 6px; margin: 0 0 12px; max-height: 96px; overflow: auto; }}
    .bank-tag-button {{ border: 1px solid #d8dde8; background: #fff; color: #344054; border-radius: 999px; padding: 5px 8px; font-size: 12px; }}
    .bank-tag-button.active {{ border-color: #0f766e; background: #eefaf7; color: #0f766e; }}
    .bank-list {{ display: grid; gap: 10px; max-height: 680px; overflow: auto; padding-right: 4px; }}
    .bank-card {{ border: 1px solid #d8dde8; border-radius: 8px; padding: 10px; background: #fff; }}
    .bank-card-head {{ display: flex; justify-content: space-between; gap: 8px; color: #667085; font-size: 12px; margin-bottom: 8px; }}
    .bank-card-title {{ font-weight: 700; color: #182230; margin-bottom: 8px; }}
    .bank-card .preview {{ border: 0; padding: 0; border-radius: 0; }}
    .bank-answer {{ margin-top: 8px; padding-top: 8px; border-top: 1px dashed #d8dde8; }}
    .bank-tags {{ display: flex; flex-wrap: wrap; gap: 5px; margin: 8px 0; }}
    .bank-tags span {{ font-size: 12px; color: #344054; background: #eef4ff; border: 1px solid #c7d7fe; border-radius: 999px; padding: 2px 7px; }}
    .bank-card-actions {{ display: flex; gap: 8px; flex-wrap: wrap; margin-top: 8px; }}
    pre {{ white-space: pre-wrap; word-break: break-word; background: #101828; color: #eef4ff; padding: 12px; border-radius: 8px; max-height: 520px; overflow: auto; }}
    .note {{ padding: 10px 12px; background: #eef4ff; border: 1px solid #c7d7fe; border-radius: 8px; margin-bottom: 14px; }}
    .message {{ padding: 10px 12px; background: #ecfdf3; border: 1px solid #abefc6; border-radius: 8px; margin-bottom: 14px; }}
    .danger {{ color: #b42318; }}
    .agent-row {{ display: grid; grid-template-columns: 1fr 110px; gap: 8px; align-items: center; }}
    .progress-wrap {{ border: 1px solid #d8dde8; background: #fff; border-radius: 8px; padding: 12px; margin-bottom: 14px; }}
    .progress-meta {{ display: grid; grid-template-columns: repeat(6, 1fr); gap: 10px; margin-bottom: 10px; }}
    .progress-meta div {{ color: #667085; font-size: 12px; }}
    .progress-meta strong {{ display: block; color: #182230; font-size: 15px; margin-top: 3px; }}
    .progress-track {{ height: 12px; border-radius: 999px; background: #e4e7ec; overflow: hidden; }}
    .progress-fill {{ height: 100%; width: {esc(run_state.get("progress", 0))}%; background: #0f766e; transition: width .3s ease; }}
    .progress-fill.running {{
      background-image: linear-gradient(45deg, rgba(255,255,255,.28) 25%, transparent 25%, transparent 50%, rgba(255,255,255,.28) 50%, rgba(255,255,255,.28) 75%, transparent 75%, transparent);
      background-size: 24px 24px;
      animation: progress-stripes .9s linear infinite;
    }}
    @keyframes progress-stripes {{
      from {{ background-position: 0 0; }}
      to {{ background-position: 24px 0; }}
    }}
    .live-log {{ max-height: 220px; margin-top: 10px; user-select: text; cursor: text; }}
    .agent-flow {{
      display: none;
      margin-top: 10px;
      border: 1px solid #d8dde8;
      border-radius: 8px;
      background: #101828;
      color: #eef4ff;
      padding: 12px;
      overflow: hidden;
    }}
    .wrap.user-mode .agent-flow {{ display: block; }}
    .wrap.user-mode .live-log {{ display: none; }}
    .agent-flow-head {{
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      gap: 12px;
      margin-bottom: 10px;
    }}
    .agent-flow-head strong {{ color: #fff; font-size: 15px; }}
    .agent-flow-head span {{ color: #98a2b3; font-size: 12px; text-align: right; }}
    .agent-flow-body {{
      display: grid;
      grid-template-columns: minmax(760px, 1fr) minmax(300px, 390px);
      gap: 12px;
      align-items: start;
    }}
    .agent-flow-canvas {{
      border: 1px solid #344054;
      background: #0f1728;
      border-radius: 8px;
      padding: 10px;
      min-height: 760px;
      position: relative;
      overflow: hidden;
    }}
    .agent-flow-map {{
      min-height: 700px;
      position: relative;
    }}
    .agent-links {{
      position: absolute;
      inset: 0;
      width: 100%;
      height: 100%;
      min-height: 700px;
      overflow: visible;
      pointer-events: none;
      opacity: .92;
      z-index: 1;
    }}
    .agent-links path {{
      fill: none;
      stroke: #94a3b8;
      stroke-width: 3.2;
      stroke-dasharray: 12 8;
      vector-effect: non-scaling-stroke;
      marker-end: url(#agent-arrow);
    }}
    .agent-links path.active {{ stroke: #5eead4; stroke-width: 4; }}
    .agent-links path.hovered {{ stroke: #fcd34d; stroke-width: 5; stroke-dasharray: none; }}
    .agent-center {{
      position: absolute;
      left: 50%;
      top: 50%;
      transform: translate(-50%, -50%);
      width: 230px;
      border: 1px solid #344054;
      background: #182230;
      border-radius: 8px;
      padding: 12px;
      text-align: center;
      color: #d0d5dd;
      box-shadow: 0 16px 40px rgba(0,0,0,.18);
      z-index: 2;
    }}
    .agent-center strong {{ display: block; color: #fff; margin-bottom: 6px; }}
    .agent-card {{
      border: 1px solid #344054;
      background: #182230;
      border-radius: 8px;
      padding: 10px;
      width: 194px;
      min-height: 146px;
      display: grid;
      grid-template-rows: auto auto auto 1fr auto;
      gap: 6px;
      position: absolute;
      z-index: 3;
      transition: border-color .2s ease, background .2s ease, transform .2s ease;
    }}
    .agent-card.reader {{ left: 50%; top: 0; transform: translateX(-50%); border-color: #2dd4bf; }}
    .agent-card.decomposer {{ right: 0; top: 30%; border-color: #93c5fd; }}
    .agent-card.reviewer {{ right: 14%; bottom: 0; border-color: #c4b5fd; }}
    .agent-card.generator {{ left: 14%; bottom: 0; border-color: #fcd34d; }}
    .agent-card.qc {{ left: 0; top: 30%; border-color: #fda4af; }}
    .agent-card.active {{
      border-color: #5eead4;
      background: #153c3b;
    }}
    .agent-card.reader.active {{ transform: translateX(-50%) translateY(-2px); }}
    .agent-card:not(.reader).active {{ transform: translateY(-2px); }}
    .agent-card.done {{ border-color: #0f766e; }}
    .agent-card.waiting {{ opacity: .72; }}
    .agent-card::after {{ display: none; }}
    .agent-icon {{
      width: 28px;
      height: 28px;
      border-radius: 50%;
      background: #23304a;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      color: #5eead4;
      font-weight: 800;
    }}
    .agent-name {{ color: #fff; font-weight: 800; font-size: 13px; }}
    .agent-role {{ color: #cbd5e1; font-size: 12px; line-height: 1.35; }}
    .agent-now {{
      border: 1px solid #344054;
      background: #101828;
      border-radius: 6px;
      padding: 6px;
      color: #eef4ff;
      font-size: 12px;
      line-height: 1.35;
      min-height: 42px;
    }}
    .agent-history {{
      display: grid;
      gap: 4px;
      align-content: start;
      color: #98a2b3;
      font-size: 11px;
      line-height: 1.3;
      max-height: 62px;
      overflow: auto;
      padding-right: 2px;
    }}
    .agent-history div {{
      border-left: 2px solid #344054;
      padding-left: 6px;
    }}
    .agent-controls {{
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
    }}
    .agent-controls button {{
      min-height: 26px;
      padding: 3px 8px;
      border-color: #344054;
      background: #101828;
      color: #eef4ff;
      font-size: 11px;
    }}
    .agent-card.active .agent-controls button {{
      border-color: #5eead4;
      color: #5eead4;
    }}
    .agent-card .step-track {{ background: #344054; }}
    .agent-card .step-fill {{ background: #5eead4; }}
    .agent-status-line {{
      margin-top: 10px;
      border: 1px solid #344054;
      background: #182230;
      border-radius: 8px;
      padding: 9px 10px;
      color: #d0d5dd;
      font-size: 13px;
      min-height: 38px;
    }}
    .interaction-board {{
      border: 1px solid #344054;
      border-radius: 8px;
      background: #182230;
      overflow: hidden;
      height: 760px;
      display: grid;
      grid-template-rows: auto minmax(0, 1fr);
    }}
    .interaction-board.collapsed {{ height: auto; grid-template-rows: auto; }}
    .interaction-board-head {{
      display: flex;
      justify-content: space-between;
      gap: 10px;
      padding: 8px 10px;
      color: #eef4ff;
      border-bottom: 1px solid #344054;
      font-size: 13px;
      font-weight: 800;
      cursor: pointer;
    }}
    .interaction-board-head span {{ color: #98a2b3; font-size: 12px; font-weight: 500; }}
    .interaction-board.collapsed .interaction-list {{ display: none; }}
    .interaction-list {{
      display: grid;
      gap: 6px;
      overflow: auto;
      padding: 8px;
      align-content: start;
    }}
    .interaction-event {{
      display: grid;
      grid-template-columns: 44px 70px minmax(0, 1fr);
      gap: 8px;
      align-items: start;
      border: 1px solid #344054;
      background: #101828;
      border-radius: 6px;
      padding: 7px 8px;
      color: #d0d5dd;
      font-size: 12px;
      cursor: default;
    }}
    .interaction-event:hover {{ border-color: #5eead4; background: #152238; }}
    .interaction-id {{ color: #5eead4; font-weight: 800; }}
    .interaction-type {{
      color: #101828;
      background: #a7f3d0;
      border-radius: 999px;
      padding: 2px 7px;
      text-align: center;
      font-weight: 800;
      font-size: 11px;
    }}
    .interaction-route {{ color: #eef4ff; font-weight: 700; }}
    .interaction-detail {{ color: #cbd5e1; line-height: 1.35; grid-column: 1 / -1; }}
    .stage-feedback {{ color: #0f766e; font-size: 13px; margin-top: 10px; min-height: 18px; }}
    .step-progress-wrap {{ margin-top: 12px; }}
    .step-title {{ display: flex; justify-content: space-between; gap: 10px; color: #344054; font-size: 13px; margin-bottom: 6px; }}
    .step-track {{ height: 8px; border-radius: 999px; background: #e4e7ec; overflow: hidden; }}
    .step-fill {{ height: 100%; width: 0%; background: #2563eb; transition: width .3s ease; }}
    .step-fill.running {{
      background-image: linear-gradient(45deg, rgba(255,255,255,.28) 25%, transparent 25%, transparent 50%, rgba(255,255,255,.28) 50%, rgba(255,255,255,.28) 75%, transparent 75%, transparent);
      background-size: 18px 18px;
      animation: progress-stripes .8s linear infinite;
    }}
    .current-step-panel {{
      border: 1px solid #d8dde8;
      border-radius: 8px;
      background: #f8fafc;
      padding: 10px 12px;
      margin-bottom: 12px;
    }}
    .current-step-panel summary {{
      cursor: pointer;
      list-style: none;
      display: flex;
      justify-content: space-between;
      gap: 10px;
      color: #344054;
      font-size: 13px;
    }}
    .current-step-panel summary::-webkit-details-marker {{ display: none; }}
    .current-step-panel summary strong {{ color: #182230; }}
    .agent-branches {{
      display: grid;
      grid-template-columns: repeat(5, minmax(0, 1fr));
      gap: 10px;
      margin-top: 10px;
    }}
    .agent-branch-title {{
      display: flex;
      justify-content: space-between;
      gap: 8px;
      font-size: 12px;
      color: #667085;
      margin-bottom: 5px;
    }}
    .progress-wrap > .step-progress-wrap {{ display: none; }}
    .current-step-panel .step-progress-wrap {{ display: block; }}
    .error-panel {{
      display: none;
      border: 1px solid #fecdca;
      background: #fef3f2;
      color: #912018;
      border-radius: 8px;
      padding: 10px 12px;
      margin: 10px 0 0;
      font-size: 13px;
      user-select: text;
    }}
    .error-panel.visible {{ display: block; }}
    .error-panel-head {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 10px;
      margin-bottom: 6px;
      color: #7a271a;
    }}
    .error-copy {{
      min-height: 28px;
      padding: 4px 10px;
      border-color: #fecdca;
      background: #fff;
      color: #912018;
      font-size: 12px;
    }}
    .error-text {{
      margin: 0;
      white-space: pre-wrap;
      overflow: auto;
      max-height: 180px;
      color: #912018;
      background: transparent;
      border: 0;
      padding: 0;
      font: inherit;
      line-height: 1.35;
      user-select: text;
      cursor: text;
    }}
    .toast-stack {{
      position: fixed;
      right: 18px;
      top: 18px;
      z-index: 9999;
      display: grid;
      gap: 8px;
      width: min(360px, calc(100vw - 36px));
      pointer-events: none;
    }}
    .toast {{
      border: 1px solid #abefc6;
      background: #ecfdf3;
      color: #074d31;
      border-radius: 8px;
      padding: 10px 12px;
      box-shadow: 0 10px 30px rgba(16, 24, 40, .12);
      font-size: 14px;
    }}
    .toast.error {{ border-color: #fecdca; background: #fef3f2; color: #912018; }}
    button.is-busy {{ opacity: .72; cursor: wait; }}
    .replay-shell {{ display: grid; gap: 14px; }}
    .machine-block {{ border: 1px solid #d8dde8; background: #f8fafc; border-radius: 8px; padding: 12px; }}
    .machine-title {{ display: flex; align-items: baseline; justify-content: space-between; gap: 12px; margin-bottom: 8px; }}
    .machine-title h3 {{ margin: 0; font-size: 16px; }}
    .machine-title span {{ color: #667085; font-size: 12px; }}
    .replay-toolbar {{ display: grid; grid-template-columns: minmax(260px, 1fr) auto auto auto; gap: 8px; align-items: end; margin-bottom: 10px; }}
    .replay-toolbar label {{ margin: 0; }}
    .replay-options {{ display: flex; gap: 12px; align-items: center; flex-wrap: wrap; color: #667085; font-size: 13px; margin-bottom: 10px; }}
    .replay-speed {{ display: grid; grid-template-columns: 70px 110px; gap: 8px; align-items: center; color: #667085; font-size: 13px; }}
    .replay-stage {{ display: grid; grid-template-columns: 260px minmax(0, 1fr); gap: 12px; }}
    .replay-timeline {{ border: 1px solid #d8dde8; border-radius: 8px; background: #fff; padding: 8px; max-height: 520px; overflow: auto; }}
    .replay-node {{ width: 100%; display: grid; grid-template-columns: 28px minmax(0, 1fr); gap: 8px; text-align: left; border: 1px solid transparent; background: transparent; color: #344054; padding: 8px; margin: 0 0 6px; border-radius: 6px; font-weight: 600; }}
    .replay-node-index {{ width: 24px; height: 24px; border-radius: 999px; background: #eef4ff; color: #344054; display: inline-flex; align-items: center; justify-content: center; font-size: 12px; }}
    .replay-node-main {{ min-width: 0; }}
    .replay-node-title {{ overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .replay-node-meta {{ color: #667085; font-size: 12px; margin-top: 3px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .replay-node.active {{ border-color: #0f766e; background: #eefaf7; color: #0f766e; }}
    .replay-node.active .replay-node-index {{ background: #0f766e; color: #fff; }}
    .replay-node.thinking {{ color: #667085; }}
    .replay-view {{ min-height: 520px; }}
    .replay-view h3 {{ margin-top: 0; font-size: 20px; }}
    .replay-meta {{ display: flex; gap: 8px; flex-wrap: wrap; color: #667085; font-size: 12px; margin-bottom: 8px; }}
    .replay-badge {{ border: 1px solid #d8dde8; background: #fff; border-radius: 999px; padding: 3px 8px; }}
    .replay-content {{ max-height: 430px; overflow: auto; }}
    .code-machine {{ margin-top: 0; }}
    .code-machine-toolbar {{ display: grid; grid-template-columns: minmax(260px, 1fr) auto auto; gap: 8px; align-items: end; margin: 8px 0; }}
    .code-machine-status {{ color: #667085; font-size: 13px; min-height: 18px; }}
    .code-snapshot-list {{ display: grid; gap: 8px; max-height: 260px; overflow: auto; margin-top: 8px; }}
    .code-snapshot {{ border: 1px solid #d8dde8; border-radius: 8px; background: #fff; padding: 9px 10px; display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 4px 10px; align-items: center; }}
    .code-snapshot strong {{ color: #182230; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .code-snapshot span {{ color: #667085; font-size: 12px; overflow-wrap: anywhere; }}
    .code-snapshot .snapshot-badge {{ border: 1px solid #d8dde8; border-radius: 999px; padding: 2px 7px; color: #344054; background: #f8fafc; }}
    @media (max-width: 1050px) {{ .header-row {{ display: block; }} .mode-controls {{ margin-top: 12px; justify-items: start; }} .wrap, .grid, .replay-stage, .replay-toolbar, .code-machine-toolbar, .agent-flow-body {{ grid-template-columns: 1fr; }} .agent-flow-canvas {{ min-height: 760px; }} .agent-flow-map {{ min-height: 680px; }} .agent-center {{ top: 42%; width: min(280px, 70%); }} .agent-card {{ width: min(260px, 44%); }} .agent-card.reader {{ top: 0; }} .agent-card.decomposer {{ right: 0; top: 24%; }} .agent-card.reviewer {{ right: 4%; bottom: 0; }} .agent-card.generator {{ left: 4%; bottom: 0; }} .agent-card.qc {{ left: 0; top: 24%; }} .interaction-board {{ min-height: 300px; }} .status {{ grid-template-columns: 1fr 1fr; }} .ocr-upload {{ grid-template-columns: 1fr; }} .ocr-upload .ocr-file {{ grid-column: auto; }} .ocr-actions {{ display: block; }} .ocr-actions button {{ width: 100%; margin-top: 8px; }} }}
  </style>
</head>
<body>
  <header>
    <div class="header-row">
      <div>
        <h1>Math Quiz Multi-Agent Workbench</h1>
        <div class="caption">编辑输入、调整参数、运行流水线，并用 LaTeX/MathJax 预览数学公式。</div>
        <div class="caption">Server: <span id="server-origin"></span></div>
        <div class="caption">Code version: <span id="code-version">{esc(current_code_signature["version"])}</span> · latest: <span id="code-latest">{esc(current_code_signature["latest_file"])}</span></div>
      </div>
      <div class="mode-controls" aria-label="Mode controls">
        <div class="mode-group">
          <span>界面</span>
          <span class="segmented">
            <button type="button" id="mode-user" onclick="setWorkbenchMode('user')">用户模式</button>
            <button type="button" id="mode-admin" onclick="setWorkbenchMode('admin')">管理模式</button>
          </span>
        </div>
        <div class="mode-group">
          <span>运行</span>
          <span class="segmented">
            <button type="button" id="engine-flash" onclick="setEngineMode('flash')">flash模式</button>
            <button type="button" id="engine-pro" onclick="setEngineMode('pro')">专业模式</button>
          </span>
        </div>
      </div>
    </div>
  </header>
  <form method="post">
    <div id="workbench-wrap" class="wrap">
      <aside data-admin-only>
        <h2>Configuration</h2>
        <label>Input markdown<input name="input" value="{esc(options["input"])}"></label>
        <label>Output directory<input name="output_dir" value="{esc(options["output_dir"])}"></label>
        <h3>Quality gates</h3>
        <label>Review threshold<input type="number" name="review_threshold" min="0" max="100" value="{esc(options["review_threshold"])}"></label>
        <label>Max review rounds<input type="number" name="max_review_rounds" min="0" max="10" value="{esc(options["max_review_rounds"])}"></label>
        <label>QC threshold<input type="number" name="qc_threshold" min="0" max="100" value="{esc(options["qc_threshold"])}"></label>
        <label>Max QC rounds<input type="number" name="max_qc_rounds" min="0" max="10" value="{esc(options["max_qc_rounds"])}"></label>
        <h3>Generation</h3>
        <label>Question count mode<select name="question_count_mode"><option {selected(profile.get("question_count_mode"), "match_source")}>match_source</option><option {selected(profile.get("question_count_mode"), "fixed")}>fixed</option></select></label>
        <label>Fixed question count<input type="number" name="question_count" min="1" max="200" value="{esc(profile.get("question_count", 1))}"></label>
        <label>Difficulty<input name="difficulty" value="{esc(profile.get("difficulty", ""))}"></label>
        <label>Knowledge points<textarea name="knowledge_points">{esc(profile.get("knowledge_points", ""))}</textarea></label>
        <label>Question types<textarea name="question_types">{esc(profile.get("question_types", ""))}</textarea></label>
        <label>Style<input name="style" value="{esc(profile.get("style", ""))}"></label>
        <label>Answer detail<select name="answer_detail"><option {selected(profile.get("answer_detail"), "concise")}>concise</option><option {selected(profile.get("answer_detail"), "detailed")}>detailed</option><option {selected(profile.get("answer_detail"), "match_input")}>match_input</option></select></label>
        <label><input type="checkbox" name="allow_input_compaction" {checked(profile.get("allow_input_compaction"))}> Allow input compaction</label>
        <h3>Models</h3>
        {model_inputs}
        {agent_inputs}
        <h3>Token budget</h3>
        {budget_inputs}
        <h3>Runtime</h3>
        {runtime_inputs}
        <h3>Result library</h3>
        {result_store_inputs}
        <button name="action" value="save_config" type="button" onclick="runAction('save_config')">Save configuration</button>
      </aside>
      <main>
        {f'<div class="message">{esc(message)}</div>' if message else ''}
        <div class="status">
          <div class="cell"><span>Run status</span><strong>{esc(state)}</strong></div>
          <div class="cell"><span>Input questions</span><strong>{esc(question_count)}</strong></div>
          <div class="cell"><span>QC score</span><strong>{esc(qc_score)}</strong></div>
          <div class="cell"><span>Output dir</span><strong>{esc(rel_path(output_dir))}</strong></div>
        </div>
        <div class="note">{esc(note)}</div>
        <div class="progress-wrap">
          <details class="current-step-panel" open>
            <summary>
              <span>Current step: <strong id="current-step-label">Round {esc(run_state.get("round", "-"))} · {esc(run_state.get("stage", "Idle"))}</strong></span>
              <span id="current-step-progress-text">{esc(run_state.get("step_progress", 0))}% expected time</span>
            </summary>
            <div class="step-progress-wrap">
              <div class="step-track"><div id="current-step-progress-fill" class="step-fill {'running' if run_state.get('running') else ''}"></div></div>
            </div>
            <div class="agent-branches">
              {agent_branch_html}
            </div>
          </details>
          <div class="progress-meta">
            <div>Stage<strong id="run-stage">{esc(run_state.get("stage", "Idle"))}</strong></div>
            <div>Round<strong id="run-round">{esc(run_state.get("round", "-"))}</strong></div>
            <div>Item<strong id="run-item">{esc(run_state.get("item_index", 0) or "-")}/{esc(run_state.get("item_total", 0) or "-")}</strong></div>
            <div>Total Progress<strong id="run-progress-text">{esc(run_state.get("progress", 0))}%</strong></div>
            <div>Started<strong id="run-started">{esc(run_state.get("started_at", "") or "-")}</strong></div>
            <div>Elapsed<strong id="run-elapsed">{esc(get_live_run_state().get("elapsed", "-"))}</strong></div>
          </div>
          <div class="progress-track"><div id="run-progress-fill" class="progress-fill {'running' if run_state.get('running') else ''}"></div></div>
          <div class="step-progress-wrap">
            <div class="step-title">
              <span id="step-label">Current step: Round {esc(run_state.get("round", "-"))} · {esc(run_state.get("stage", "Idle"))}</span>
              <span id="step-progress-text">{esc(run_state.get("step_progress", 0))}%</span>
            </div>
            <div class="step-track"><div id="step-progress-fill" class="step-fill {'running' if run_state.get('running') else ''}"></div></div>
          </div>
          <div id="stage-feedback" class="stage-feedback"></div>
          <div id="error-panel" class="error-panel {'visible' if run_state.get("error_message") else ''}">
            <div class="error-panel-head">
              <strong>错误提示</strong>
              <button type="button" class="secondary error-copy" onclick="copyErrorPanel()">Copy</button>
            </div>
            <pre id="error-panel-text" class="error-text">{esc(run_state.get("error_message", ""))}</pre>
          </div>
          <div id="agent-flow" class="agent-flow">
            <div class="agent-flow-head">
              <strong>Multi-Agent Collaboration</strong>
              <span id="agent-flow-summary">等待开始。每个智能体会接力处理同一道题。</span>
            </div>
            <div class="agent-flow-body">
              <div class="agent-flow-canvas">
                <div class="agent-flow-map">
                  <svg id="agent-links-svg" class="agent-links" aria-hidden="true">
                    <defs>
                      <marker id="agent-arrow" viewBox="0 0 10 10" refX="9.5" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse">
                        <path d="M 0 0 L 10 5 L 0 10 z" fill="#94a3b8"></path>
                      </marker>
                    </defs>
                    <path id="link-reader-decomposer" d="M 610 82 C 760 72, 890 126, 872 206" marker-end="url(#agent-arrow)"></path>
                    <path id="link-decomposer-reviewer" d="M 874 360 C 850 452, 770 512, 676 514" marker-end="url(#agent-arrow)"></path>
                    <path id="link-reviewer-generator" d="M 560 548 C 500 572, 420 572, 360 548" marker-end="url(#agent-arrow)"></path>
                    <path id="link-generator-qc" d="M 278 514 C 164 492, 108 398, 128 314" marker-end="url(#agent-arrow)"></path>
                    <path id="link-qc-reader" d="M 128 206 C 116 104, 324 60, 390 82" marker-end="url(#agent-arrow)"></path>
                  </svg>
                  <div class="agent-center">
                    <strong id="agent-flow-center-title">Current Collaboration</strong>
                    <span id="agent-flow-status">尚未开始运行。</span>
                  </div>
                  <div class="agent-card reader waiting" id="flow-reader" data-flow-agent="Reader">
                    <div class="agent-icon">R</div>
                    <div class="agent-name">Reader</div>
                    <div class="agent-role">整理输入，按题目拆分，建立可读上下文。</div>
                    <div class="agent-now" id="flow-reader-now">On progress: waiting for input.</div>
                    <div class="agent-history" id="flow-reader-history"></div>
                    <div class="agent-controls"><button type="button" onclick="runAction('pause_pipeline', this, 'Reader')">Pause</button><button type="button" onclick="runAction('resume_pipeline', this, 'Reader')">Resume</button></div>
                    <div class="step-track"><div id="flow-reader-fill" class="step-fill"></div></div>
                  </div>
                  <div class="agent-card decomposer waiting" id="flow-decomposer" data-flow-agent="Decomposer">
                    <div class="agent-icon">D</div>
                    <div class="agent-name">Decomposer</div>
                    <div class="agent-role">抽取知识点、结构、难度与生成约束。</div>
                    <div class="agent-now" id="flow-decomposer-now">On progress: waiting for Reader.</div>
                    <div class="agent-history" id="flow-decomposer-history"></div>
                    <div class="agent-controls"><button type="button" onclick="runAction('pause_pipeline', this, 'Decomposer')">Pause</button><button type="button" onclick="runAction('resume_pipeline', this, 'Decomposer')">Resume</button></div>
                    <div class="step-track"><div id="flow-decomposer-fill" class="step-fill"></div></div>
                  </div>
                  <div class="agent-card reviewer waiting" id="flow-reviewer" data-flow-agent="Reviewer">
                    <div class="agent-icon">V</div>
                    <div class="agent-name">Reviewer</div>
                    <div class="agent-role">复核结构与限制，提醒后续生成风险。</div>
                    <div class="agent-now" id="flow-reviewer-now">On progress: waiting for Decomposer.</div>
                    <div class="agent-history" id="flow-reviewer-history"></div>
                    <div class="agent-controls"><button type="button" onclick="runAction('pause_pipeline', this, 'Reviewer')">Pause</button><button type="button" onclick="runAction('resume_pipeline', this, 'Reviewer')">Resume</button></div>
                    <div class="step-track"><div id="flow-reviewer-fill" class="step-fill"></div></div>
                  </div>
                  <div class="agent-card generator waiting" id="flow-generator" data-flow-agent="Generator">
                    <div class="agent-icon">G</div>
                    <div class="agent-name">Generator</div>
                    <div class="agent-role">生成相似数学题与答案，保持 LaTeX 表达。</div>
                    <div class="agent-now" id="flow-generator-now">On progress: waiting for review notes.</div>
                    <div class="agent-history" id="flow-generator-history"></div>
                    <div class="agent-controls"><button type="button" onclick="runAction('pause_pipeline', this, 'Generator')">Pause</button><button type="button" onclick="runAction('resume_pipeline', this, 'Generator')">Resume</button></div>
                    <div class="step-track"><div id="flow-generator-fill" class="step-fill"></div></div>
                  </div>
                  <div class="agent-card qc waiting" id="flow-qc" data-flow-agent="QC">
                    <div class="agent-icon">Q</div>
                    <div class="agent-name">QC</div>
                    <div class="agent-role">检查题目、答案、格式与覆盖质量。</div>
                    <div class="agent-now" id="flow-qc-now">On progress: waiting for generated output.</div>
                    <div class="agent-history" id="flow-qc-history"></div>
                    <div class="agent-controls"><button type="button" onclick="runAction('pause_pipeline', this, 'QC')">Pause</button><button type="button" onclick="runAction('resume_pipeline', this, 'QC')">Resume</button></div>
                    <div class="step-track"><div id="flow-qc-fill" class="step-fill"></div></div>
                  </div>
                </div>
              </div>
              <div id="interaction-board" class="interaction-board collapsed">
                <div class="interaction-board-head" onclick="toggleInteractionTimeline()">
                  <strong>Interaction Timeline</strong>
                  <span><span id="interaction-count">0 events</span> · <span id="interaction-toggle-label">展开</span></span>
                </div>
                <div id="interaction-list" class="interaction-list">
                  <div class="interaction-event">
                    <span class="interaction-id">#000</span>
                    <span class="interaction-type">idle</span>
                    <span class="interaction-route">UI</span>
                    <span class="interaction-detail">等待运行后展示 Agent 调用、返回、修订与完成事件。</span>
                  </div>
                </div>
              </div>
            </div>
          </div>
          <pre id="run-live-log" class="live-log">{esc(run_state.get("log", "") or "Pipeline log will appear here after Run pipeline starts.")}</pre>
        </div>
        <div class="grid">
          <section class="panel">
            <h2>Input</h2>
            <details class="ocr-panel">
              <summary>
                <strong>OCR / Document input</strong>
                <span>支持图片、PDF 与 Word 文档，结果可预览后再写入 Source</span>
              </summary>
              <div class="ocr-body">
                <div class="ocr-upload">
                  <div id="ocr-dropzone" class="ocr-dropzone" role="button" tabindex="0">
                    <strong>Drag files here</strong>
                    <span>支持图片、PDF、Word（.docx）；也可以点击此处选择文件。</span>
                  </div>
                  <label class="ocr-file">Images / PDF / Word<input id="ocr-images" type="file" accept="image/*,.pdf,.docx,.doc,application/vnd.openxmlformats-officedocument.wordprocessingml.document,application/msword" multiple></label>
                  <label>OCR engine<select id="ocr-provider"><option value="paddleocr">PaddleOCR-VL</option><option value="tesseract">Tesseract local</option><option value="textin">TextIn erase + Tesseract</option><option value="youdao">Youdao erase + Tesseract</option><option value="textin_erase">TextIn erase only</option><option value="youdao_erase">Youdao erase only</option></select></label>
                  <label>Language<input id="ocr-lang" value="chi_sim+eng"></label>
                  <label>Result target<select id="ocr-target"><option value="preview">Preview first</option><option value="source">Replace Source after OCR</option><option value="append">Append to Source after OCR</option></select></label>
                </div>
                <div class="ocr-actions">
                  <div id="ocr-status" class="ocr-status">选择图片、PDF 或 Word 后运行导入。默认先预览，确认后再写入 Source。</div>
                  <button class="secondary" type="button" id="ocr-button" onclick="runOcrUpload()">Run import</button>
                  <button type="button" id="ocr-run-button" onclick="runOcrUpload({{ autoRun: true }})">OCR + Run</button>
                </div>
                <div id="ocr-progress" class="ocr-progress">
                  <div class="ocr-progress-head">
                    <span id="ocr-progress-label">OCR waiting</span>
                    <span id="ocr-progress-text">0%</span>
                  </div>
                  <div class="ocr-progress-track"><div id="ocr-progress-fill" class="ocr-progress-fill"></div></div>
                </div>
                <div id="ocr-result" class="ocr-result">
                  <div class="ocr-result-head">
                    <strong id="ocr-result-title">OCR result</strong>
                    <span id="ocr-result-provider" class="ocr-pill">-</span>
                  </div>
                  <div id="ocr-result-meta" class="ocr-result-meta"></div>
                  <div id="ocr-result-preview" class="preview ocr-result-preview"><p>No OCR source preview yet.</p></div>
                  <div class="ocr-result-actions">
                    <button type="button" class="secondary" onclick="applyOcrResult('replace')">Replace Source</button>
                    <button type="button" class="secondary" onclick="applyOcrResult('append')">Append to Source</button>
                    <button type="button" class="secondary" onclick="toggleOcrSourcePreview()">Show original</button>
                    <button type="button" class="secondary" onclick="showInputMode('source')">Open Source</button>
                  </div>
                  <div id="ocr-question-picker" class="ocr-question-picker">
                    <div class="ocr-question-head">
                      <span id="ocr-question-title">选择导入题目</span>
                      <div class="ocr-question-tools">
                        <button type="button" class="secondary" onclick="setAllOcrQuestions(true)">All</button>
                        <button type="button" class="secondary" onclick="setAllOcrQuestions(false)">None</button>
                      </div>
                    </div>
                    <div class="caption">在上方原图/PDF 中对照题号，从 OCR 题目树中选择需要导入的题目。</div>
                    <div id="ocr-question-list" class="ocr-question-list"></div>
                    <div class="ocr-result-actions">
                      <button type="button" class="secondary" onclick="applySelectedOcrQuestions('replace')">Replace with selected</button>
                      <button type="button" class="secondary" onclick="applySelectedOcrQuestions('append')">Append selected</button>
                    </div>
                  </div>
                  <details class="ocr-text-details">
                    <summary>识别文本 / Markdown / LaTeX</summary>
                    <div id="ocr-text-preview" class="preview ocr-text-preview"><p>No OCR text yet.</p></div>
                  </details>
                  <div id="ocr-source-view" class="ocr-source-view"></div>
                </div>
              </div>
            </details>
            <div class="input-viewbar">
              <button type="button" id="input-preview-button" class="secondary active" onclick="showInputMode('preview')">Preview</button>
              <button type="button" id="input-source-button" class="secondary" onclick="showInputMode('source')">Source</button>
            </div>
            <div id="input-preview" class="preview input-preview {'placeholder' if not input_text.strip() else ''}" spellcheck="false">{md_to_html(input_text) if input_text.strip() else '请在 Source 中输入数学题 Markdown / LaTeX 内容，Preview 仅用于查看。'}</div>
            <textarea id="input-source" class="input-editor hidden" name="input_text" placeholder="请输入数学题 Markdown / LaTeX 内容，或直接粘贴题目文本。">{esc(input_text)}</textarea>
            <div class="actions">
              <button name="action" value="save_input" type="button" onclick="runAction('save_input')">Save input</button>
              <button class="secondary" data-admin-only name="action" value="coverage" type="button" onclick="runAction('coverage')">Run coverage</button>
              <button class="secondary" data-admin-only name="action" value="show_options" type="button" onclick="runAction('show_options')">Show options</button>
              <button name="action" value="pipeline" type="button" onclick="runAction('pipeline')">Run pipeline</button>
              <button class="secondary" name="action" value="pause_pipeline" type="button" onclick="runAction('pause_pipeline')">Pause</button>
              <button class="secondary" name="action" value="resume_pipeline" type="button" onclick="runAction('resume_pipeline')">Resume</button>
              <button class="secondary danger-button" name="action" value="stop_pipeline" type="button" onclick="runAction('stop_pipeline')">Stop</button>
              <button class="secondary" data-admin-only name="action" value="tests" type="button" onclick="runAction('tests')">Local tests</button>
              <button class="secondary" data-admin-only name="action" value="preview" type="button" onclick="runAction('preview')">Render preview</button>
            </div>
            <h2 data-admin-only>Command log</h2>
            <div data-admin-only class="caption">{esc(LAST_COMMAND["cmd"] or "No command has been run in this UI session.")}</div>
            <pre data-admin-only>exit code: {esc(LAST_COMMAND["code"])}

{esc(LAST_COMMAND["stdout"])}
{esc(LAST_COMMAND["stderr"])}</pre>
          </section>
          <section class="panel">
            <h2>Output</h2>
            <div class="tabs">
              <button type="button" class="tab-button active" onclick="showTab('questions', this)">Questions</button>
              <button type="button" class="tab-button" onclick="showTab('answers', this)">Answer Key</button>
              <button type="button" class="tab-button" onclick="showTab('question-bank', this)">题库</button>
              <button type="button" class="tab-button" onclick="showTab('bank-similar', this)">题库中类似题</button>
              <button type="button" class="tab-button" onclick="showTab('qc', this)">QC Result</button>
              <button type="button" class="tab-button" onclick="showTab('timemachine', this)">时光机</button>
              <button type="button" data-admin-only class="tab-button" onclick="showTab('preview', this)">Preview</button>
              <button type="button" data-admin-only class="tab-button" onclick="showTab('coverage', this)">Coverage</button>
              <button type="button" data-admin-only class="tab-button" onclick="showTab('debug', this)">Debug</button>
            </div>
            <div id="questions" class="tab active"><div id="questions-preview" class="preview">{md_to_html(questions_text)}</div></div>
            <div id="answers" class="tab"><div id="answers-preview" class="preview">{md_to_html(answers_text)}</div></div>
            <div id="question-bank" class="tab">
              <div class="bank-toolbar">
                <label>搜索题库<input id="bank-query" placeholder="输入知识点、题型、题干关键词或 LaTeX 片段"></label>
                <label>标签<select id="bank-tag"></select></label>
                <button type="button" class="secondary" onclick="loadQuestionBank()">搜索题库</button>
              </div>
              <div id="bank-tag-cloud" class="bank-tag-cloud"></div>
              <div id="bank-summary" class="bank-summary"></div>
              <div id="bank-list" class="bank-list"></div>
            </div>
            <div id="bank-similar" class="tab">
              <div class="actions compact-actions">
                <button type="button" class="secondary" onclick="runAction('bank_similar')">刷新相似题</button>
              </div>
              <div id="bank-similar-preview" class="preview">{md_to_html(bank_similar_text)}</div>
            </div>
            <div id="qc" class="tab">{qc_html}</div>
            <div id="timemachine" class="tab">
              <div class="replay-shell">
                <div class="machine-block">
                  <div class="machine-title">
                    <h3>运行回放</h3>
                    <span>按 Agent 节点查看一次生成过程</span>
                  </div>
                  <div class="replay-toolbar">
                    <label>记录<select id="replay-select">{replay_options}</select></label>
                    <button type="button" class="secondary" onclick="loadReplay()">载入</button>
                    <button type="button" onclick="playReplay()">播放</button>
                    <button type="button" class="secondary" onclick="nextReplayNode()">下一步</button>
                  </div>
                  <div class="replay-options">
                    <label><input type="checkbox" id="replay-skip-thinking" checked onchange="renderReplayTimeline()">只看关键节点</label>
                    <div class="replay-speed"><span>播放速度</span><select id="replay-speed"><option value="1600">1x</option><option value="900">2x</option><option value="450">4x</option><option value="180">8x</option></select></div>
                    <button type="button" class="secondary" onclick="pauseReplay()">暂停</button>
                  </div>
                  <div class="replay-stage">
                    <div id="replay-timeline" class="replay-timeline"></div>
                    <div id="replay-view" class="preview replay-view"><p>选择一条记录后点击“载入”。</p></div>
                  </div>
                </div>
                <div class="machine-block code-machine">
                <div class="machine-title">
                  <h3>代码同步快照</h3>
                  <span>跟随代码版本自动保存</span>
                </div>
                <div class="caption">恢复会覆盖对应代码文件；恢复后请重启 UI，让服务器载入恢复后的代码。</div>
                <div class="code-machine-toolbar">
                  <label>代码快照<select id="code-snapshot-select"></select></label>
                  <button type="button" class="secondary" onclick="saveCodeSnapshot()">保存当前代码</button>
                  <button type="button" class="danger-button" onclick="restoreCodeSnapshot()">恢复此快照</button>
                </div>
                <div id="code-machine-status" class="code-machine-status">正在读取代码快照...</div>
                <div id="code-snapshot-list" class="code-snapshot-list"></div>
                </div>
              </div>
            </div>
            <div id="preview" data-admin-only class="tab"><div id="render-preview" class="preview">{md_to_html(preview_text)}</div></div>
            <div id="coverage" data-admin-only class="tab"><div class="status">{coverage_cards}</div><pre>{esc(json.dumps(coverage, ensure_ascii=False, indent=2))}</pre></div>
            <div id="debug" data-admin-only class="tab"><pre>{esc(debug_text)}</pre></div>
          </section>
        </div>
      </main>
    </div>
  </form>
  <script>
    const initialInputText = {input_text_json};
    const initialCodeSignature = {code_signature_json};
    let replayData = {replay_json};
    let codeTimeMachine = {code_time_machine_json};
    let questionBankData = {bank_payload_json};
    let currentReplay = null;
    let replayNodes = [];
    let replayIndex = 0;
    let replayTimer = null;
    document.getElementById('server-origin').textContent = window.location.href;
    let inputMode = 'preview';
    let latestInputText = initialInputText;
    const inputPlaceholder = 'Edit math Markdown / LaTeX in Source. Preview is read-only.';
    let lastRunning = false;
    let reloadedAfterFinish = false;
    let pendingCodeReload = false;
    let logUserPaused = false;
    let latestOcrText = '';
    let latestOcrResult = null;
    let latestOcrQuestions = [];
    let latestOcrUploads = [];
    let selectedPipelineItemIds = '';
    let ocrProgressTimer = null;
    let ocrProgressValue = 0;
    let liveQuestionsMtime = 0;
    let liveAnswersMtime = 0;
    let outputResetAt = 0;
    const toastStack = document.createElement('div');
    toastStack.className = 'toast-stack';
    document.body.appendChild(toastStack);
    let workbenchMode = localStorage.getItem('mathQuizWorkbenchMode') || 'admin';
    let engineMode = localStorage.getItem('mathQuizEngineMode') || 'flash';

    function showToast(text, kind = 'success') {{
      const toast = document.createElement('div');
      toast.className = 'toast' + (kind === 'error' ? ' error' : '');
      toast.textContent = text;
      toastStack.appendChild(toast);
      setTimeout(() => {{
        toast.style.opacity = '0';
        toast.style.transform = 'translateY(-4px)';
        toast.style.transition = 'opacity .2s ease, transform .2s ease';
      }}, 2600);
      setTimeout(() => toast.remove(), 3100);
    }}

    function applyModes() {{
      const userMode = workbenchMode === 'user';
      const wrap = document.getElementById('workbench-wrap');
      if (wrap) wrap.classList.toggle('user-mode', userMode);
      document.querySelectorAll('[data-admin-only]').forEach((el) => {{
        el.classList.toggle('mode-hidden', userMode);
        if (userMode && el.classList.contains('active')) el.classList.remove('active');
      }});
      document.getElementById('mode-user').classList.toggle('active', userMode);
      document.getElementById('mode-admin').classList.toggle('active', !userMode);
      document.getElementById('engine-flash').classList.toggle('active', engineMode === 'flash');
      document.getElementById('engine-pro').classList.toggle('active', engineMode === 'pro');
      if (userMode) {{
        document.querySelectorAll('.tab').forEach(el => el.classList.remove('active'));
        document.querySelectorAll('.tab-button').forEach(el => el.classList.remove('active'));
        const questions = document.getElementById('questions');
        const firstButton = document.querySelector('.tab-button:not(.mode-hidden)');
        if (questions) questions.classList.add('active');
        if (firstButton) firstButton.classList.add('active');
      }}
    }}

    function escapeHtml(value) {{
      return String(value || '')
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;');
    }}

    function refreshReplaySelect() {{
      const select = document.getElementById('replay-select');
      if (!select || !replayData || !Array.isArray(replayData.runs)) return;
      const current = select.value;
      select.innerHTML = replayData.runs.map(run => {{
        const score = run.summary && run.summary.qc_score !== null && run.summary.qc_score !== undefined ? ' · score ' + run.summary.qc_score : '';
        return '<option value="' + escapeHtml(run.id) + '">' + escapeHtml((run.updated_at || '') + ' · ' + (run.title || run.path || '') + score) + '</option>';
      }}).join('');
      if (current) select.value = current;
    }}

    function filteredReplayNodes() {{
      const skip = document.getElementById('replay-skip-thinking')?.checked;
      if (!currentReplay) return [];
      const nodes = currentReplay.nodes || [];
      return skip ? nodes.filter(node => node.kind !== 'thinking') : nodes;
    }}

    function renderReplayTimeline() {{
      replayNodes = filteredReplayNodes();
      if (replayIndex >= replayNodes.length) replayIndex = Math.max(0, replayNodes.length - 1);
      const timeline = document.getElementById('replay-timeline');
      if (!timeline) return;
      if (!currentReplay) {{
        timeline.innerHTML = '<p>暂无载入记录。</p>';
        return;
      }}
      timeline.innerHTML = replayNodes.map((node, index) => {{
        const active = index === replayIndex ? ' active' : '';
        const thinking = node.kind === 'thinking' ? ' thinking' : '';
        const meta = (node.kind || 'key') + ' · ' + (node.time || '-') + ' · ' + (node.path || '-');
        return '<button type="button" class="replay-node' + active + thinking + '" onclick="showReplayNode(' + index + ')">'
          + '<span class="replay-node-index">' + (index + 1) + '</span>'
          + '<span class="replay-node-main">'
          + '<span class="replay-node-title">' + escapeHtml((node.stage || '-') + ' · ' + (node.title || node.path || '-')) + '</span>'
          + '<span class="replay-node-meta">' + escapeHtml(meta) + '</span>'
          + '</span>'
          + '</button>';
      }}).join('') || '<p>跳过思考后没有可播放节点。</p>';
      showReplayNode(replayIndex, false);
    }}

    function showReplayNode(index, rerender = true) {{
      replayIndex = Math.max(0, Math.min(index || 0, replayNodes.length - 1));
      const node = replayNodes[replayIndex];
      const view = document.getElementById('replay-view');
      if (!view || !node) {{
        if (view) view.innerHTML = '<p>没有可显示的节点。</p>';
        return;
      }}
      const content = node.path && node.path.endsWith('.md')
        ? clientMarkdownToHtml(node.preview || '')
        : '<pre class="replay-content">' + escapeHtml(node.preview || '') + '</pre>';
      view.innerHTML = [
        '<h3>' + escapeHtml(node.stage || '-') + ' · ' + escapeHtml(node.title || '') + '</h3>',
        '<div class="replay-meta">',
        '<span class="replay-badge">' + escapeHtml(node.kind || 'key') + '</span>',
        '<span class="replay-badge">' + escapeHtml(node.time || '-') + '</span>',
        '<span class="replay-badge">' + escapeHtml(node.path || '-') + '</span>',
        '</div>',
        content
      ].join('');
      if (rerender) renderReplayTimeline();
      typesetMath([view]);
    }}

    async function loadReplay() {{
      pauseReplay();
      try {{
        const response = await fetch('/replay?ts=' + Date.now());
        const fresh = await response.json();
        if (fresh && fresh.runs) replayData = fresh;
        refreshReplaySelect();
      }} catch (error) {{}}
      const select = document.getElementById('replay-select');
      const id = select ? select.value : '';
      currentReplay = (replayData.runs || []).find(run => run.id === id) || (replayData.runs || [])[0] || null;
      replayIndex = 0;
      renderReplayTimeline();
      if (currentReplay) showToast('时光机已载入：' + (currentReplay.title || currentReplay.path || 'record'));
    }}

    function playReplay() {{
      if (!currentReplay) loadReplay();
      pauseReplay();
      const speed = Number(document.getElementById('replay-speed')?.value || 1600);
      replayTimer = setInterval(() => {{
        if (!replayNodes.length || replayIndex >= replayNodes.length - 1) {{
          pauseReplay();
          return;
        }}
        showReplayNode(replayIndex + 1);
      }}, speed);
    }}

    function pauseReplay() {{
      if (replayTimer) clearInterval(replayTimer);
      replayTimer = null;
    }}

    function nextReplayNode() {{
      pauseReplay();
      if (!currentReplay) loadReplay();
      if (replayNodes.length) showReplayNode(Math.min(replayIndex + 1, replayNodes.length - 1));
    }}

    function renderCodeTimeMachine() {{
      const select = document.getElementById('code-snapshot-select');
      const list = document.getElementById('code-snapshot-list');
      const status = document.getElementById('code-machine-status');
      const snapshots = (codeTimeMachine && codeTimeMachine.snapshots) || [];
      if (select) {{
        const current = select.value;
        select.innerHTML = snapshots.map(snapshot => {{
          const label = (snapshot.created_at || '-') + ' · ' + (snapshot.version || '-') + ' · ' + (snapshot.latest_file || '-');
          return '<option value="' + escapeHtml(snapshot.id || '') + '">' + escapeHtml(label) + '</option>';
        }}).join('');
        if (current) select.value = current;
      }}
      if (list) {{
        list.innerHTML = snapshots.map(snapshot => {{
          return [
            '<div class="code-snapshot">',
            '<strong>' + escapeHtml(snapshot.created_at || '-') + '</strong>',
            '<span class="snapshot-badge">' + escapeHtml(snapshot.reason || '-') + '</span>',
            '<span>版本 ' + escapeHtml(snapshot.version || '-') + '</span>',
            '<span>' + escapeHtml(snapshot.file_count || 0) + ' 个文件 · 最新 ' + escapeHtml(snapshot.latest_file || '-') + '</span>',
            '</div>'
          ].join('');
        }}).join('') || '<p>暂无代码快照。</p>';
      }}
      if (status) {{
        const currentSnapshot = codeTimeMachine && codeTimeMachine.current;
        status.textContent = currentSnapshot && currentSnapshot.version
          ? '当前代码已同步：' + currentSnapshot.created_at + ' · ' + currentSnapshot.file_count + ' 个文件'
          : '尚未保存当前代码快照。';
      }}
    }}

    async function refreshCodeTimeMachine() {{
      const response = await fetch('/code-time-machine?ts=' + Date.now(), {{ cache: 'no-store' }});
      const data = await response.json();
      if (!data.ok) throw new Error(data.message || 'Failed to load code snapshots.');
      codeTimeMachine = data;
      renderCodeTimeMachine();
      return data;
    }}

    async function saveCodeSnapshot() {{
      try {{
        const response = await fetch('/code-time-machine', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ action: 'snapshot' }})
        }});
        const data = await response.json();
        if (!response.ok || !data.ok) throw new Error(data.message || 'Snapshot failed.');
        codeTimeMachine = data;
        renderCodeTimeMachine();
        showToast('代码快照已保存。');
      }} catch (error) {{
        showToast('代码快照保存失败。', 'error');
        setErrorPanel(error.message);
      }}
    }}

    async function restoreCodeSnapshot() {{
      const select = document.getElementById('code-snapshot-select');
      const snapshotId = select ? select.value : '';
      if (!snapshotId) {{
        showToast('请选择一个代码快照。', 'error');
        return;
      }}
      if (!window.confirm('恢复代码快照会覆盖当前代码文件。确定恢复？')) return;
      try {{
        const response = await fetch('/code-time-machine', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ action: 'restore', id: snapshotId }})
        }});
        const data = await response.json();
        if (!response.ok || !data.ok) throw new Error(data.message || 'Restore failed.');
        codeTimeMachine = data;
        renderCodeTimeMachine();
        showToast('代码快照已恢复，请重启 UI 载入恢复后的服务器代码。');
      }} catch (error) {{
        showToast('代码快照恢复失败。', 'error');
        setErrorPanel(error.message);
      }}
    }}

    function setWorkbenchMode(mode) {{
      workbenchMode = mode === 'user' ? 'user' : 'admin';
      localStorage.setItem('mathQuizWorkbenchMode', workbenchMode);
      applyModes();
      scheduleAgentLinkUpdate();
      showToast(workbenchMode === 'user' ? '已切换到用户模式。' : '已切换到管理模式。');
    }}

    function setEngineMode(mode) {{
      engineMode = mode === 'pro' ? 'pro' : 'flash';
      localStorage.setItem('mathQuizEngineMode', engineMode);
      applyModes();
      scheduleAgentLinkUpdate();
      showToast(engineMode === 'pro' ? '已切换到专业模式。' : '已切换到 flash 模式。');
    }}

    function updateAgentFlow(data) {{
      const stage = data.stage || 'Idle';
      const agentProgress = data.agent_progress || {{}};
      const agents = ['Reader', 'Decomposer', 'Reviewer', 'Generator', 'QC'];
      const roleWaiting = {{
        Reader: 'On progress: waiting for input.',
        Decomposer: 'On progress: waiting for Reader.',
        Reviewer: 'On progress: waiting for Decomposer.',
        Generator: 'On progress: waiting for review notes.',
        QC: 'On progress: waiting for generated output.'
      }};
      const roleActive = {{
        Reader: 'On progress: 正在标准化输入、切分题目，并准备可交给后续 Agent 的上下文。',
        Decomposer: 'On progress: 正在拆解知识点、结构、难度和生成约束。',
        Reviewer: 'On progress: 正在审查 Decomposer 的结构结果，并决定是否返回修订。',
        Generator: 'On progress: 正在根据结构约束生成题目、答案与 LaTeX 表达。',
        QC: 'On progress: 正在验证数学一致性、答案质量和格式。'
      }};
      const stageMap = {{ 'Reader item': 'Reader' }};
      const activeAgent = stageMap[stage] || (agents.includes(stage) ? stage : '');
      const events = buildAgentInteractionEvents(data);
      const history = agentHistoryFromEvents(events, agents);
      for (const agent of agents) {{
        const key = agent.toLowerCase();
        const card = document.getElementById('flow-' + key);
        const fill = document.getElementById('flow-' + key + '-fill');
        const now = document.getElementById('flow-' + key + '-now');
        const done = document.getElementById('flow-' + key + '-history');
        const value = Math.max(0, Math.min(100, Number(agentProgress[agent] || 0)));
        if (fill) {{
          fill.style.width = value + '%';
          fill.classList.toggle('running', !!data.running && !data.paused && activeAgent === agent);
        }}
        if (now) {{
          if (data.running && activeAgent === agent) {{
            now.textContent = roleActive[agent];
          }} else if (value >= 100) {{
            now.textContent = 'On progress: 当前轮任务已完成，等待下游 Agent 反馈。';
          }} else {{
            now.textContent = roleWaiting[agent];
          }}
        }}
        if (done) {{
          const items = (history[agent] || []).slice(-4);
          done.innerHTML = items.length
            ? items.map(item => '<div>' + escapeHtml(item) + '</div>').join('')
            : '<div>尚无已完成动作。</div>';
        }}
        if (card) {{
          card.classList.toggle('active', !!data.running && activeAgent === agent);
          card.classList.toggle('done', value >= 100 && activeAgent !== agent);
          card.classList.toggle('waiting', value <= 0 && activeAgent !== agent);
        }}
      }}
      const itemLabel = data.item_total ? ('第 ' + (data.item_index || '-') + '/' + data.item_total + ' 题') : '整份输入';
      const summary = document.getElementById('agent-flow-summary');
      const status = document.getElementById('agent-flow-status');
      const centerTitle = document.getElementById('agent-flow-center-title');
      if (summary) {{
        summary.textContent = data.paused
          ? itemLabel + ' · Paused at ' + (data.pause_owner || activeAgent || stage)
          : data.running
          ? itemLabel + ' · Round ' + (data.round || '-') + ' · ' + (activeAgent || stage) + ' 正在协作'
          : (stage === 'Complete' ? '协作完成，结果已输出。' : '等待开始。每个智能体会接力处理同一道题。');
      }}
      if (centerTitle) centerTitle.textContent = data.paused ? 'Paused' : (activeAgent ? activeAgent + ' in progress' : 'Current Collaboration');
      if (!status) return;
      if (data.paused) {{
        status.textContent = '流程已暂停在 ' + (data.pause_owner || activeAgent || stage || '当前步骤') + '。点击任意 Agent 卡片里的 Resume 继续。';
      }} else if (!data.running && stage === 'Complete') {{
        status.textContent = '五个智能体已完成接力：读取、拆解、复核、生成与质检。';
      }} else if (!data.running && stage === 'Failed') {{
        status.textContent = '流程中断。请查看错误提示，管理模式可查看完整日志。';
      }} else if (!data.running) {{
        status.textContent = '尚未开始运行。';
      }} else if (activeAgent === 'Reader') {{
        status.textContent = 'Reader 正在把原始试卷整理成可传递给后续 Agent 的题目上下文。';
      }} else if (activeAgent === 'Decomposer') {{
        status.textContent = 'Decomposer 正在把题目拆成知识点、结构和生成约束，交给 Reviewer 复核。';
      }} else if (activeAgent === 'Reviewer') {{
        status.textContent = 'Reviewer 正在检查拆解结果，降低后续生成偏题或超限的风险。';
      }} else if (activeAgent === 'Generator') {{
        status.textContent = 'Generator 正在基于前两位 Agent 的结构信息生成新题与答案。';
      }} else if (activeAgent === 'QC') {{
        status.textContent = 'QC 正在检查数学一致性、答案质量与格式，必要时反馈修正。';
      }} else {{
        status.textContent = stage + ' 正在运行。';
      }}
      updateAgentLinks(events, activeAgent, data.running, stage);
      renderInteractionTimeline(events);
    }}

    function linkIdForRoute(fromAgent, toAgent) {{
      const direct = {{
        'Reader>Decomposer': 'link-reader-decomposer',
        'Decomposer>Reviewer': 'link-decomposer-reviewer',
        'Reviewer>Generator': 'link-reviewer-generator',
        'Generator>QC': 'link-generator-qc',
        'QC>Reader': 'link-qc-reader',
        'QC>UI': 'link-qc-reader'
      }};
      return direct[(fromAgent || '') + '>' + (toAgent || '')] || '';
    }}

    function agentEdgePoint(card, mapRect, toward) {{
      const rect = card.getBoundingClientRect();
      const cx = rect.left - mapRect.left + rect.width / 2;
      const cy = rect.top - mapRect.top + rect.height / 2;
      const tx = toward.x - cx;
      const ty = toward.y - cy;
      const halfW = rect.width / 2 + 6;
      const halfH = rect.height / 2 + 6;
      const scale = Math.max(Math.abs(tx) / halfW, Math.abs(ty) / halfH, 1);
      return {{ x: cx + tx / scale, y: cy + ty / scale }};
    }}

    function scheduleAgentLinkUpdate() {{
      requestAnimationFrame(() => {{
        updateAgentLinkGeometry();
        requestAnimationFrame(updateAgentLinkGeometry);
      }});
      setTimeout(updateAgentLinkGeometry, 80);
      setTimeout(updateAgentLinkGeometry, 250);
    }}

    function updateAgentLinkGeometry() {{
      const map = document.querySelector('.agent-flow-map');
      const svg = document.getElementById('agent-links-svg');
      if (!map || !svg) return;
      const mapRect = map.getBoundingClientRect();
      const width = Math.max(mapRect.width || map.offsetWidth || 980, 760);
      const height = Math.max(mapRect.height || map.offsetHeight || 700, 680);
      if (!width || !height) return;
      svg.setAttribute('viewBox', '0 0 ' + width + ' ' + height);
      svg.setAttribute('width', String(width));
      svg.setAttribute('height', String(height));
      const pairs = [
        ['reader', 'decomposer', 'link-reader-decomposer'],
        ['decomposer', 'reviewer', 'link-decomposer-reviewer'],
        ['reviewer', 'generator', 'link-reviewer-generator'],
        ['generator', 'qc', 'link-generator-qc'],
        ['qc', 'reader', 'link-qc-reader']
      ];
      for (const [fromKey, toKey, linkId] of pairs) {{
        const from = document.getElementById('flow-' + fromKey);
        const to = document.getElementById('flow-' + toKey);
        const path = document.getElementById(linkId);
        if (!from || !to || !path) continue;
        const fromRect = from.getBoundingClientRect();
        const toRect = to.getBoundingClientRect();
        const fromCenter = {{
          x: fromRect.left - mapRect.left + fromRect.width / 2,
          y: fromRect.top - mapRect.top + fromRect.height / 2
        }};
        const toCenter = {{
          x: toRect.left - mapRect.left + toRect.width / 2,
          y: toRect.top - mapRect.top + toRect.height / 2
        }};
        const start = agentEdgePoint(from, mapRect, toCenter);
        const end = agentEdgePoint(to, mapRect, fromCenter);
        if (linkId === 'link-generator-qc') {{
          const outsideX = Math.max(20, Math.min(start.x, end.x) - 135);
          const c1 = {{ x: outsideX, y: start.y }};
          const c2 = {{ x: outsideX, y: end.y }};
          path.setAttribute('d', 'M ' + start.x.toFixed(1) + ' ' + start.y.toFixed(1) + ' C ' + c1.x.toFixed(1) + ' ' + c1.y.toFixed(1) + ', ' + c2.x.toFixed(1) + ' ' + c2.y.toFixed(1) + ', ' + end.x.toFixed(1) + ' ' + end.y.toFixed(1));
          continue;
        }}
        if (linkId === 'link-reader-decomposer') {{
          const outsideY = Math.max(18, Math.min(start.y, end.y) - 85);
          path.setAttribute('d', 'M ' + start.x.toFixed(1) + ' ' + start.y.toFixed(1) + ' C ' + (start.x + 120).toFixed(1) + ' ' + outsideY.toFixed(1) + ', ' + (end.x - 120).toFixed(1) + ' ' + outsideY.toFixed(1) + ', ' + end.x.toFixed(1) + ' ' + end.y.toFixed(1));
          continue;
        }}
        if (linkId === 'link-qc-reader') {{
          const outsideY = Math.max(18, Math.min(start.y, end.y) - 85);
          path.setAttribute('d', 'M ' + start.x.toFixed(1) + ' ' + start.y.toFixed(1) + ' C ' + (start.x + 60).toFixed(1) + ' ' + outsideY.toFixed(1) + ', ' + (end.x - 120).toFixed(1) + ' ' + outsideY.toFixed(1) + ', ' + end.x.toFixed(1) + ' ' + end.y.toFixed(1));
          continue;
        }}
        if (linkId === 'link-decomposer-reviewer') {{
          const outsideX = Math.min(width - 20, Math.max(start.x, end.x) + 115);
          path.setAttribute('d', 'M ' + start.x.toFixed(1) + ' ' + start.y.toFixed(1) + ' C ' + outsideX.toFixed(1) + ' ' + start.y.toFixed(1) + ', ' + outsideX.toFixed(1) + ' ' + end.y.toFixed(1) + ', ' + end.x.toFixed(1) + ' ' + end.y.toFixed(1));
          continue;
        }}
        const dx = end.x - start.x;
        const dy = end.y - start.y;
        const distance = Math.max(Math.hypot(dx, dy), 1);
        const bend = Math.max(50, Math.min(140, distance * 0.28));
        const normal = {{ x: -dy / distance, y: dx / distance }};
        const c1 = {{ x: start.x + dx * 0.35 + normal.x * bend, y: start.y + dy * 0.35 + normal.y * bend }};
        const c2 = {{ x: start.x + dx * 0.65 + normal.x * bend, y: start.y + dy * 0.65 + normal.y * bend }};
        path.setAttribute('d', 'M ' + start.x.toFixed(1) + ' ' + start.y.toFixed(1) + ' C ' + c1.x.toFixed(1) + ' ' + c1.y.toFixed(1) + ', ' + c2.x.toFixed(1) + ' ' + c2.y.toFixed(1) + ', ' + end.x.toFixed(1) + ' ' + end.y.toFixed(1));
      }}
    }}

    function updateAgentLinks(events, activeAgent, running, stage) {{
      scheduleAgentLinkUpdate();
      document.querySelectorAll('.agent-links path').forEach(path => path.classList.remove('active', 'hovered'));
      let activeLink = '';
      for (let index = events.length - 1; index >= 0; index--) {{
        activeLink = linkIdForRoute(events[index].from, events[index].to);
        if (activeLink) break;
      }}
      if (!activeLink && running) {{
        const fallback = {{
          Decomposer: 'link-reader-decomposer',
          Reviewer: 'link-decomposer-reviewer',
          Generator: 'link-reviewer-generator',
          QC: 'link-generator-qc',
          Reader: 'link-qc-reader'
        }};
        activeLink = fallback[activeAgent] || '';
      }}
      if (!activeLink && stage === 'Complete') activeLink = 'link-qc-reader';
      const link = activeLink ? document.getElementById(activeLink) : null;
      if (link) link.classList.add('active');
    }}

    function highlightAgentLink(linkId, force) {{
      document.querySelectorAll('.agent-links path').forEach(path => path.classList.remove('hovered'));
      if (!force || !linkId) return;
      const link = document.getElementById(linkId);
      if (link) link.classList.add('hovered');
    }}

    function toggleInteractionTimeline() {{
      const board = document.getElementById('interaction-board');
      const label = document.getElementById('interaction-toggle-label');
      if (!board) return;
      const collapsed = !board.classList.contains('collapsed');
      board.classList.toggle('collapsed', collapsed);
      if (label) label.textContent = collapsed ? '展开' : '收起';
      scheduleAgentLinkUpdate();
    }}

    function eventAgentKey(name) {{
      const agents = ['Reader', 'Decomposer', 'Reviewer', 'Generator', 'QC'];
      return agents.includes(name) ? name : '';
    }}

    function addInteraction(events, type, fromAgent, toAgent, detail) {{
      events.push({{
        id: events.length + 1,
        type,
        from: fromAgent || 'UI',
        to: toAgent || '',
        detail
      }});
    }}

    function buildAgentInteractionEvents(data) {{
      const events = [];
      const lines = String(data.log || '').split(/\\r?\\n/).map(line => line.trim()).filter(Boolean);
      for (const line of lines) {{
        let match = line.match(/^===\\s*Round\\s+(\\d+):\\s+running\\s+(Reader|Decomposer|Reviewer|Generator|QC)\\s*===/i);
        if (match) {{
          const agent = match[2][0].toUpperCase() + match[2].slice(1);
          const routeFrom = agent === 'Reader' ? 'UI' : agent === 'Decomposer' ? 'Reader' : agent === 'Reviewer' ? 'Decomposer' : agent === 'Generator' ? 'Reviewer' : 'Generator';
          addInteraction(events, 'call', routeFrom, agent, 'Round ' + match[1] + ' 调用 ' + agent + '。');
          continue;
        }}
        match = line.match(/^===\\s*Reader\\s+item\\s+(\\d+)\\/(\\d+):\\s+running\\s+full\\s+pipeline\\s*===/i);
        if (match) {{
          addInteraction(events, 'call', 'Reader', 'Decomposer', 'Reader 将第 ' + match[1] + '/' + match[2] + ' 题交给下游 Agent。');
          continue;
        }}
        match = line.match(/^Reviewer result:\\s*is_valid=(\\w+),\\s*score=([\\d.]+)/i);
        if (match) {{
          addInteraction(events, 'return', 'Reviewer', 'Decomposer', 'Reviewer 返回结构审查：valid=' + match[1] + ', score=' + match[2] + '。');
          continue;
        }}
        match = line.match(/^QC result:\\s*is_passed=(\\w+),\\s*score=([\\d.]+)/i);
        if (match) {{
          addInteraction(events, 'return', 'QC', 'Generator', 'QC 返回质量审查：passed=' + match[1] + ', score=' + match[2] + '。');
          continue;
        }}
        match = line.match(/^Reviewer failed;\\s*revising\\s+Decomposer\\s+for\\s+round\\s+(\\d+)/i);
        if (match) {{
          addInteraction(events, 'revise', 'Reviewer', 'Decomposer', 'Reviewer 要求 Decomposer 进入 round ' + match[1] + ' 修订。');
          continue;
        }}
        match = line.match(/^QC failed;\\s*revising\\s+Generator\\s+for\\s+round\\s+(\\d+)/i);
        if (match) {{
          addInteraction(events, 'revise', 'QC', 'Generator', 'QC 要求 Generator 进入 round ' + match[1] + ' 修订。');
          continue;
        }}
        if (/Reviewer passed/i.test(line)) {{
          addInteraction(events, 'accept', 'Reviewer', 'Generator', 'Reviewer 通过结构结果，允许进入生成。');
          continue;
        }}
        if (/QC passed/i.test(line)) {{
          addInteraction(events, 'accept', 'QC', 'UI', 'QC 通过生成结果，准备输出。');
          continue;
        }}
        if (/Pipeline complete/i.test(line)) {{
          addInteraction(events, 'done', 'QC', 'Reader', 'Pipeline complete，最终结果写入输出，流程回到开头等待下一次输入。');
          continue;
        }}
      }}
      if (!events.length && data.running) {{
        const agent = eventAgentKey(data.stage) || (data.stage === 'Reader item' ? 'Reader' : '');
        addInteraction(events, 'status', 'UI', agent || 'Pipeline', (agent || data.stage || 'Pipeline') + ' 正在运行，等待下一条交互日志。');
      }}
      return events.slice(-30).map((event, index) => ({{ ...event, id: index + 1 }}));
    }}

    function agentHistoryFromEvents(events, agents) {{
      const history = {{}};
      for (const agent of agents) history[agent] = [];
      for (const event of events) {{
        if (history[event.from]) history[event.from].push('#' + String(event.id).padStart(3, '0') + ' ' + event.type + ': ' + event.detail);
        if (history[event.to]) history[event.to].push('#' + String(event.id).padStart(3, '0') + ' received from ' + event.from + '.');
      }}
      return history;
    }}

    function renderInteractionTimeline(events) {{
      const list = document.getElementById('interaction-list');
      const count = document.getElementById('interaction-count');
      if (!list) return;
      if (count) count.textContent = events.length + ' events';
      if (!events.length) {{
        list.innerHTML = '<div class="interaction-event"><span class="interaction-id">#000</span><span class="interaction-type">idle</span><span class="interaction-route">UI</span><span class="interaction-detail">等待运行后展示 Agent 调用、返回、修订与完成事件。</span></div>';
        return;
      }}
      list.innerHTML = events.map(event => {{
        const route = event.to ? event.from + ' → ' + event.to : event.from;
        const linkId = linkIdForRoute(event.from, event.to);
        return [
          '<div class="interaction-event" data-link-id="' + escapeHtml(linkId) + '" onmouseenter="highlightAgentLink(this.dataset.linkId, true)" onmouseleave="highlightAgentLink(this.dataset.linkId, false)">',
          '<span class="interaction-id">#' + String(event.id).padStart(3, '0') + '</span>',
          '<span class="interaction-type">' + escapeHtml(event.type) + '</span>',
          '<span class="interaction-route">' + escapeHtml(route) + '</span>',
          '<span class="interaction-detail">' + escapeHtml(event.detail) + '</span>',
          '</div>'
        ].join('');
      }}).join('');
      list.scrollTop = list.scrollHeight;
    }}

    function setErrorPanel(text) {{
      const errorPanel = document.getElementById('error-panel');
      const errorText = document.getElementById('error-panel-text');
      if (!errorPanel) return;
      const cleaned = cleanErrorText(text || '');
      if (cleaned) {{
        if (errorText) errorText.textContent = cleaned;
        errorPanel.classList.add('visible');
      }} else {{
        if (errorText) errorText.textContent = '';
        errorPanel.classList.remove('visible');
      }}
    }}

    async function copyErrorPanel() {{
      const errorText = document.getElementById('error-panel-text');
      const text = errorText ? errorText.textContent : '';
      if (!text.trim()) {{
        showToast('没有可复制的错误内容。', 'error');
        return;
      }}
      try {{
        await navigator.clipboard.writeText(text);
        showToast('错误内容已复制。');
      }} catch (error) {{
        const selection = window.getSelection();
        const range = document.createRange();
        range.selectNodeContents(errorText);
        selection.removeAllRanges();
        selection.addRange(range);
        showToast('已选中错误内容，可按 Ctrl+C 复制。');
      }}
    }}

    function cleanErrorText(text) {{
      if (!text) return '';
      if (text.includes('MissingDateHeader') || text.includes('x-bce-date')) {{
        return [
          'PaddleOCR result download was rejected by the BOS result URL because API auth headers were attached.',
          'The UI now fetches the signed result URL without Authorization headers.',
          'Please retry OCR. If it still fails, check whether the PaddleOCR result link expired or the token changed.',
          'Original message:',
          text
        ].join('\\n');
      }}
      if (text.includes('WinError 10054') || text.includes('forcibly closed by the remote host')) {{
        return [
          'DeepSeek API request was interrupted: the remote host forcibly closed the connection.',
          'This is usually a transient network/proxy/API gateway disconnect, not a UI formatting error.',
          'Retry the run, and if it repeats check proxy/firewall/VPN stability and DeepSeek API availability.',
          'Full traceback is preserved in the command log below.'
        ].join('\\n');
      }}
      if (text.includes('getaddrinfo failed') || text.includes('urlopen error')) {{
        return [
          'DeepSeek API request failed because the network/DNS lookup failed.',
          'Check internet/proxy/firewall settings and verify DEEPSEEK_API_BASE in .env.',
          'Full traceback is preserved in the command log below.'
        ].join('\\n');
      }}
      return text
        .split('\\n')
        .map(line => line.trim())
        .filter(line => line && !line.toLowerCase().startsWith('traceback') && !line.toLowerCase().startsWith('raise ') && !line.toLowerCase().startsWith('the above exception') && !line.toLowerCase().includes(' from error'))
        .slice(-5)
        .join('\\n');
    }}

    document.querySelector('form').addEventListener('submit', (event) => {{
      event.preventDefault();
    }});

    function clientMarkdownToHtml(text) {{
      const escapeHtml = (value) => value
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;');
      const joinPreviewLines = (left, right) => {{
        if (!left) return right;
        if (!right) return left;
        if (/[\u4e00-\u9fff]$/.test(left) && /^[\u4e00-\u9fff，。；：！？、）】》]/.test(right)) return left + right;
        if (/^[,.;:!?)]/.test(right)) return left + right;
        return left + ' ' + right;
      }};
      const normalizePreviewText = (value) => {{
        const lines = (value || '').replaceAll('\\r\\n', '\\n').replaceAll('\\r', '\\n').split('\\n');
        const merged = [];
        let paragraph = '';
        let inFence = false;
        let inMathBlock = false;
        const flushParagraph = () => {{
          if (paragraph) {{
            merged.push(paragraph);
            paragraph = '';
          }}
        }};
        for (const raw of lines) {{
          const stripped = raw.trim();
          const structural = !stripped
            || stripped.startsWith('# ')
            || stripped.startsWith('## ')
            || stripped.startsWith('### ')
            || stripped.startsWith('- ')
            || stripped.startsWith('* ')
            || stripped.startsWith('> ')
            || stripped.startsWith('|')
            || stripped.startsWith('```')
            || /^\\d+[.)]\\s+/.test(stripped)
            || stripped === '$$'
            || stripped === '\\\\['
            || stripped === '\\\\]'
            || stripped.startsWith('$$')
            || stripped.endsWith('$$');
          if (stripped.startsWith('```')) {{
            flushParagraph();
            merged.push(raw);
            inFence = !inFence;
            continue;
          }}
          if (inFence) {{
            merged.push(raw);
            continue;
          }}
          if (stripped === '$$' || stripped === '\\\\[') {{
            flushParagraph();
            merged.push(raw);
            inMathBlock = true;
            continue;
          }}
          if ((stripped === '$$' || stripped === '\\\\]') && inMathBlock) {{
            merged.push(raw);
            inMathBlock = false;
            continue;
          }}
          if (inMathBlock) {{
            merged.push(raw);
            continue;
          }}
          if (structural) {{
            flushParagraph();
            merged.push(raw);
          }} else {{
            paragraph = joinPreviewLines(paragraph, stripped);
          }}
        }}
        flushParagraph();
        return merged.join('\\n');
      }};
      const lines = normalizePreviewText(text || 'No content yet.').split('\\n');
      let html = '';
      let inList = false;
      let inMathBlock = false;
      let mathLines = [];
      for (const raw of lines) {{
        const line = raw.trim();
        if ((line === '$$' || line === '\\\\[') && !inMathBlock) {{
          if (inList) {{ html += '</ul>'; inList = false; }}
          inMathBlock = true;
          mathLines = [line];
        }} else if (inMathBlock) {{
          mathLines.push(escapeHtml(raw));
          if (line === '$$' || line === '\\\\]') {{
            html += '<div class="math-block">' + mathLines.join('\\n') + '</div>';
            inMathBlock = false;
            mathLines = [];
          }}
        }} else if (line.startsWith('### ')) {{
          if (inList) {{ html += '</ul>'; inList = false; }}
          html += '<h3>' + escapeHtml(line.slice(4)) + '</h3>';
        }} else if (line.startsWith('## ')) {{
          if (inList) {{ html += '</ul>'; inList = false; }}
          html += '<h2>' + escapeHtml(line.slice(3)) + '</h2>';
        }} else if (line.startsWith('# ')) {{
          if (inList) {{ html += '</ul>'; inList = false; }}
          html += '<h1>' + escapeHtml(line.slice(2)) + '</h1>';
        }} else if (line.startsWith('- ')) {{
          if (!inList) {{ html += '<ul>'; inList = true; }}
          html += '<li>' + escapeHtml(line.slice(2)) + '</li>';
        }} else if (!line) {{
          if (inList) {{ html += '</ul>'; inList = false; }}
          continue;
        }} else {{
          if (inList) {{ html += '</ul>'; inList = false; }}
          html += '<p>' + escapeHtml(raw).replaceAll('**', '') + '</p>';
        }}
      }}
      if (inList) html += '</ul>';
      if (inMathBlock && mathLines.length) html += '<div class="math-block">' + mathLines.join('\\n') + '</div>';
      return html;
    }}

    function renderInputPreview() {{
      const preview = document.getElementById('input-preview');
      if ((latestInputText || '').trim()) {{
        preview.classList.remove('placeholder');
        preview.innerHTML = clientMarkdownToHtml(latestInputText);
      }} else {{
        preview.classList.add('placeholder');
        preview.textContent = inputPlaceholder;
      }}
      typesetMath([preview]);
    }}

    function showInputMode(mode) {{
      inputMode = mode;
      const source = document.getElementById('input-source');
      const preview = document.getElementById('input-preview');
      document.getElementById('input-preview-button').classList.toggle('active', mode === 'preview');
      document.getElementById('input-source-button').classList.toggle('active', mode === 'source');
      if (mode === 'preview') {{
        latestInputText = source.value;
        renderInputPreview();
      }}
      source.classList.toggle('hidden', mode !== 'source');
      preview.classList.toggle('hidden', mode !== 'preview');
    }}

    function syncInputFromVisible() {{
      const source = document.getElementById('input-source');
      if (inputMode === 'source') {{
        latestInputText = source.value;
      }} else {{
        source.value = latestInputText;
      }}
      return latestInputText;
    }}

    function resetOutputAfterInputChange() {{
      outputResetAt = 0;
    }}

    function bankRecordText(record) {{
      const stem = (record.stem || '').trim();
      const answer = (record.answer || '').trim();
      return answer ? stem + '\\n\\n答案：\\n' + answer : stem;
    }}

    function renderQuestionBank(payload = questionBankData) {{
      questionBankData = payload || {{}};
      const summary = document.getElementById('bank-summary');
      const list = document.getElementById('bank-list');
      const tagSelect = document.getElementById('bank-tag');
      const tagCloud = document.getElementById('bank-tag-cloud');
      if (!summary || !list) return;
      const index = questionBankData.index || {{}};
      const records = questionBankData.records || [];
      const topTags = questionBankData.top_tags || [];
      if (tagSelect && !tagSelect.dataset.ready) {{
        tagSelect.innerHTML = '<option value="">全部标签</option>' + topTags.map(tag =>
          '<option value="' + escapeHtml(tag.name || '') + '">' + escapeHtml((tag.name || '未命名标签') + (tag.count ? ' · ' + tag.count : '')) + '</option>'
        ).join('');
        tagSelect.dataset.ready = '1';
      }}
      if (tagCloud && !tagCloud.dataset.ready) {{
        tagCloud.innerHTML = topTags.slice(0, 36).map(tag =>
          '<button type="button" class="bank-tag-button" data-tag="' + escapeHtml(tag.name || '') + '" onclick="chooseBankTag(this)">'
          + escapeHtml(tag.name || '未命名标签') + (tag.count ? ' · ' + escapeHtml(tag.count) : '') + '</button>'
        ).join('');
        tagCloud.dataset.ready = '1';
      }}
      summary.innerHTML = [
        '<span>题库：' + escapeHtml(index.bank_count || '-') + ' 个</span>',
        '<span>题目：' + escapeHtml(index.question_count || records.length || 0) + ' 道</span>',
        '<span>当前显示：' + escapeHtml(records.length) + ' 道</span>',
        '<span>导入时间：' + escapeHtml(index.imported_at || '-') + '</span>'
      ].join('');
      if (!questionBankData.ok) {{
        list.innerHTML = '<div class="note danger">' + escapeHtml(questionBankData.message || 'Question bank is not ready.') + '</div>';
        return;
      }}
      if (questionBankData.lazy) {{
        list.innerHTML = '<div class="note">题库已接入。输入关键词后点“搜索题库”，或直接搜索查看前 40 道题。</div>';
        return;
      }}
      if (!records.length) {{
        list.innerHTML = '<div class="note">没有匹配题目。换一个关键词，或先导入题库。</div>';
        return;
      }}
      list.innerHTML = records.map((record, index) => {{
        const meta = [record.grade, record.type, record.difficulty].filter(Boolean).join(' · ');
        const tags = (record.tags || []).map(tag => '<span>' + escapeHtml(tag) + '</span>').join('');
        const stem = clientMarkdownToHtml(record.stem || '');
        const answer = record.answer
          ? '<details class="bank-answer"><summary>答案 / 解析</summary><div class="preview">' + clientMarkdownToHtml(record.answer || '') + '</div></details>'
          : '';
        return '<article class="bank-card" data-bank-index="' + index + '">' +
          '<div class="bank-card-head"><span>' + escapeHtml(meta || '题库题目') + '</span><span>第 ' + escapeHtml(index + 1) + ' 题</span></div>' +
          '<div class="preview bank-card-title">' + stem + '</div>' +
          '<div class="bank-tags">' + tags + '</div>' +
          answer +
          '<div class="bank-card-actions">' +
            '<button type="button" class="secondary" onclick="applyBankQuestion(' + index + ', \\'append\\')">追加到输入</button>' +
            '<button type="button" class="secondary" onclick="applyBankQuestion(' + index + ', \\'replace\\')">替换输入</button>' +
          '</div>' +
        '</article>';
      }}).join('');
      typesetMath(Array.from(list.querySelectorAll('.preview')));
    }}

    function chooseBankTag(button) {{
      const tag = button?.dataset?.tag || '';
      const select = document.getElementById('bank-tag');
      if (select) select.value = tag;
      document.querySelectorAll('.bank-tag-button').forEach(item => item.classList.toggle('active', item.dataset.tag === tag));
      loadQuestionBank();
    }}

    async function loadQuestionBank() {{
      const query = document.getElementById('bank-query')?.value || '';
      const tag = document.getElementById('bank-tag')?.value || '';
      const list = document.getElementById('bank-list');
      if (list) list.innerHTML = '<div class="note">正在读取题库...</div>';
      try {{
        const url = new URL('/question-bank', window.location.origin);
        url.searchParams.set('query', query);
        url.searchParams.set('tag', tag);
        url.searchParams.set('limit', '40');
        const response = await fetch(url.href, {{ cache: 'no-store' }});
        if (!response.ok) throw new Error('HTTP ' + response.status);
        const payload = await response.json();
        renderQuestionBank(payload);
        showToast('题库已刷新。', payload.ok ? 'success' : 'error');
      }} catch (error) {{
        renderQuestionBank({{ ok: false, message: '题库读取失败：' + error.message, records: [] }});
        showToast('题库读取失败：' + error.message, 'error');
      }}
    }}

    function applyBankQuestion(index, mode) {{
      const record = (questionBankData.records || [])[index];
      if (!record) {{
        showToast('没有找到这道题。', 'error');
        return;
      }}
      const text = bankRecordText(record).trim();
      if (!text) {{
        showToast('这道题没有可导入内容。', 'error');
        return;
      }}
      const source = document.getElementById('input-source');
      if (mode === 'append' && latestInputText.trim()) {{
        latestInputText = latestInputText.replace(/\\s*$/, '') + '\\n\\n' + text;
      }} else {{
        latestInputText = text;
      }}
      source.value = latestInputText;
      showInputMode('preview');
      resetOutputAfterInputChange();
      showToast(mode === 'append' ? '已追加题库题目。' : '已替换为题库题目。', 'success');
    }}

    document.getElementById('input-source').addEventListener('input', (event) => {{
      latestInputText = event.target.value;
      resetOutputAfterInputChange();
    }});

    document.getElementById('ocr-images').addEventListener('change', (event) => {{
      const count = event.target.files ? event.target.files.length : 0;
      const status = document.getElementById('ocr-status');
      status.textContent = count
        ? count + ' file(s) selected. Run import when ready.'
        : '选择图片、PDF 或 Word 后运行导入。默认先预览，确认后再写入 Source。';
    }});

    const ocrDropzone = document.getElementById('ocr-dropzone');
    const ocrFileInput = document.getElementById('ocr-images');
    function setOcrFiles(files) {{
      const accepted = Array.from(files || []).filter(file => {{
        const name = (file.name || '').toLowerCase();
        const type = (file.type || '').toLowerCase();
        return type.startsWith('image/')
          || name.endsWith('.pdf')
          || name.endsWith('.docx')
          || name.endsWith('.doc')
          || type === 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
          || type === 'application/msword';
      }});
      if (!accepted.length) {{
        showToast('Only image, PDF, and Word files are supported here.', 'error');
        return;
      }}
      const transfer = new DataTransfer();
      accepted.forEach(file => transfer.items.add(file));
      ocrFileInput.files = transfer.files;
      ocrFileInput.dispatchEvent(new Event('change', {{ bubbles: true }}));
      showToast(accepted.length + ' file(s) ready.');
    }}
    if (ocrDropzone && ocrFileInput) {{
      ocrDropzone.addEventListener('click', () => ocrFileInput.click());
      ocrDropzone.addEventListener('keydown', (event) => {{
        if (event.key === 'Enter' || event.key === ' ') {{
          event.preventDefault();
          ocrFileInput.click();
        }}
      }});
      ['dragenter', 'dragover'].forEach(name => {{
        ocrDropzone.addEventListener(name, (event) => {{
          event.preventDefault();
          ocrDropzone.classList.add('dragging');
        }});
      }});
      ['dragleave', 'drop'].forEach(name => {{
        ocrDropzone.addEventListener(name, (event) => {{
          event.preventDefault();
          ocrDropzone.classList.remove('dragging');
        }});
      }});
      ocrDropzone.addEventListener('drop', (event) => {{
        setOcrFiles(event.dataTransfer ? event.dataTransfer.files : []);
      }});
    }}

    function applyOcrResult(mode) {{
      if (!latestOcrText) {{
        showToast('No import result to apply yet.', 'error');
        return;
      }}
      const source = document.getElementById('input-source');
      if (mode === 'append' && source.value.trim()) {{
        latestInputText = source.value.replace(/\\s*$/, '') + '\\n\\n' + latestOcrText;
      }} else {{
        latestInputText = latestOcrText;
      }}
      source.value = latestInputText;
      showInputMode('source');
      resetOutputAfterInputChange();
      showToast(mode === 'append' ? 'Import result appended to Source.' : 'Import result copied to Source.');
    }}

    function setOcrProgress(percent, label, running = true) {{
      ocrProgressValue = Math.max(0, Math.min(100, Math.round(percent || 0)));
      const wrap = document.getElementById('ocr-progress');
      const fill = document.getElementById('ocr-progress-fill');
      const text = document.getElementById('ocr-progress-text');
      const labelEl = document.getElementById('ocr-progress-label');
      if (wrap) wrap.classList.add('visible');
      if (fill) {{
        fill.style.width = ocrProgressValue + '%';
        fill.classList.toggle('running', !!running);
      }}
      if (text) text.textContent = ocrProgressValue + '%';
      if (labelEl && label) labelEl.textContent = label;
    }}

    function startOcrProgress() {{
      if (ocrProgressTimer) clearInterval(ocrProgressTimer);
      const started = Date.now();
      const messages = [
        [0, 'Preparing upload...'],
        [8, 'Uploading file(s)...'],
        [18, 'Reading file content...'],
        [34, 'Extracting document/OCR text...'],
        [58, 'Building question tree...'],
        [78, 'Cleaning Markdown / LaTeX...'],
        [90, 'Waiting for final import result...']
      ];
      setOcrProgress(3, messages[0][1], true);
      ocrProgressTimer = setInterval(() => {{
        const elapsed = (Date.now() - started) / 1000;
        let target = elapsed < 8 ? 8 + elapsed * 3 : 32 + Math.log(elapsed - 6) * 18;
        target = Math.min(94, target);
        ocrProgressValue = Math.max(ocrProgressValue, target);
        let label = messages[0][1];
        for (const [threshold, message] of messages) {{
          if (ocrProgressValue >= threshold) label = message;
        }}
        setOcrProgress(ocrProgressValue, label, true);
      }}, 700);
    }}

    function finishOcrProgress(ok, label) {{
      if (ocrProgressTimer) clearInterval(ocrProgressTimer);
      ocrProgressTimer = null;
      setOcrProgress(ok ? 100 : Math.max(ocrProgressValue, 100), label || (ok ? 'Import completed.' : 'Import failed.'), false);
    }}

    function selectedOcrQuestionText() {{
      const selected = Array.from(document.querySelectorAll('.ocr-question-check:checked')).map(input => input.value);
      const picked = latestOcrQuestions.filter(item => selected.includes(item.id));
      return picked.map(item => item.text || item.label || '').filter(Boolean).join('\\n\\n');
    }}

    function selectedOcrQuestionIds() {{
      const selected = Array.from(document.querySelectorAll('.ocr-question-check:checked')).map(input => input.value);
      return latestOcrQuestions
        .filter(item => selected.includes(item.id))
        .map((item, index) => item.reader_item_id || item.item_id || item.number || (index + 1))
        .filter(Boolean)
        .join(',');
    }}

    function updateOcrQuestionTitle() {{
      const title = document.getElementById('ocr-question-title');
      if (!title) return;
      const total = document.querySelectorAll('.ocr-question-check').length;
      const selected = document.querySelectorAll('.ocr-question-check:checked').length;
      title.textContent = '选择导入题目 · ' + selected + '/' + total;
      selectedPipelineItemIds = selectedOcrQuestionIds();
    }}

    function setAllOcrQuestions(checked) {{
      document.querySelectorAll('.ocr-question-check').forEach(input => {{
        input.checked = !!checked;
      }});
      updateOcrQuestionTitle();
    }}

    function applySelectedOcrQuestions(mode) {{
      const text = selectedOcrQuestionText();
      if (!text) {{
        showToast('请选择至少一道题。', 'error');
        return;
      }}
      const source = document.getElementById('input-source');
      if (mode === 'append' && source.value.trim()) {{
        latestInputText = source.value.replace(/\\s*$/, '') + '\\n\\n' + text;
      }} else {{
        latestInputText = text;
      }}
      source.value = latestInputText;
      selectedPipelineItemIds = selectedOcrQuestionIds();
      showInputMode('source');
      resetOutputAfterInputChange();
      showToast(mode === 'append' ? 'Selected questions appended.' : 'Selected questions copied to Source.');
    }}

    function renderOcrQuestions(questions) {{
      latestOcrQuestions = questions || [];
      const picker = document.getElementById('ocr-question-picker');
      const list = document.getElementById('ocr-question-list');
      if (!picker || !list) return;
      if (!latestOcrQuestions.length) {{
        picker.classList.remove('visible');
        list.innerHTML = '';
        return;
      }}
      picker.classList.add('visible');
      list.innerHTML = latestOcrQuestions.map((item, index) => {{
        const pages = item.pages && item.pages.length ? ' · page ' + item.pages.join(',') : '';
        const label = (item.label || ('Question ' + (index + 1))) + pages;
        const hint = item.text ? item.text.replace(/\\s+/g, ' ').slice(0, 120) : '';
        return [
          '<label class="ocr-question-option">',
          '<input class="ocr-question-check" type="checkbox" value="' + escapeHtml(item.id || '') + '" checked onchange="updateOcrQuestionTitle()">',
          '<span>' + escapeHtml(label) + (hint ? '<small>' + escapeHtml(hint) + '</small>' : '') + '</span>',
          '</label>'
        ].join('');
      }}).join('');
      updateOcrQuestionTitle();
    }}

    function ocrOriginalPreviewHtml(file) {{
      const url = '/local-file?path=' + encodeURIComponent(file.path || '');
      const name = (file.name || file.path || '').toLowerCase();
      if ((file.mime || '').startsWith('image/') || /\\.(png|jpg|jpeg|bmp|webp|tif|tiff)$/.test(name)) {{
        return '<img alt="OCR source preview" src="' + url + '">';
      }} else if ((file.mime || '') === 'application/pdf' || name.endsWith('.pdf')) {{
        return '<iframe title="OCR PDF preview" src="' + url + '"></iframe>';
      }} else if (/\\.(docx|doc)$/.test(name)) {{
        return '<div class="note"><strong>Word document</strong><br><a href="' + url + '" target="_blank" rel="noreferrer">' + escapeHtml(file.name || file.path || '') + '</a><br><span class="caption">文档文本已提取到下方预览，可直接写入 Source。</span></div>';
      }}
      return '<p class="caption">This file type cannot be previewed inline: ' + escapeHtml(file.name || file.path || '') + '</p>';
    }}

    function toggleOcrSourcePreview() {{
      const view = document.getElementById('ocr-source-view');
      if (!view) return;
      if (view.classList.contains('visible')) {{
        view.classList.remove('visible');
        return;
      }}
      if (!latestOcrUploads.length) {{
        showToast('No original file to preview.', 'error');
        return;
      }}
      view.innerHTML = latestOcrUploads.map(ocrOriginalPreviewHtml).join('');
      view.classList.add('visible');
    }}

    function renderOcrResult(data) {{
      latestOcrText = data.text || '';
      latestOcrResult = data.result || {{}};
      latestOcrUploads = data.uploaded || [];
      const panel = document.getElementById('ocr-result');
      const title = document.getElementById('ocr-result-title');
      const provider = document.getElementById('ocr-result-provider');
      const meta = document.getElementById('ocr-result-meta');
      const preview = document.getElementById('ocr-result-preview');
      const textPreview = document.getElementById('ocr-text-preview');
      if (!panel || !preview) return;
      panel.classList.add('visible');
      if (title) title.textContent = 'Import result · ' + (latestOcrUploads.length || latestOcrResult.image_count || 0) + ' file(s)';
      if (provider) provider.textContent = latestOcrResult.provider || '-';
      const warnings = latestOcrResult.warnings || [];
      const outputs = [
        latestOcrResult.clean_output ? 'clean: ' + latestOcrResult.clean_output : '',
        latestOcrResult.structured_output ? 'structured: ' + latestOcrResult.structured_output : '',
        latestOcrResult.question_tree_output ? 'question tree: ' + latestOcrResult.question_tree_output : ''
      ].filter(Boolean);
      if (meta) {{
        meta.innerHTML = [
          '<span class="ocr-pill">' + escapeHtml(latestOcrResult.lang || '-') + '</span>',
          ...outputs.map(item => '<span class="ocr-pill">' + escapeHtml(item) + '</span>'),
          ...warnings.map(item => '<span class="ocr-pill ocr-warning">' + escapeHtml(item) + '</span>')
        ].join('');
      }}
      if (latestOcrUploads.length) {{
        preview.innerHTML = latestOcrUploads.map(ocrOriginalPreviewHtml).join('');
      }} else {{
        preview.innerHTML = '<p>No original file preview available.</p>';
      }}
      if (textPreview) {{
        textPreview.innerHTML = clientMarkdownToHtml(latestOcrText || 'No text returned.');
        typesetMath([textPreview]);
      }}
      renderOcrQuestions(data.questions || []);
    }}

    async function runOcrUpload(options = {{}}) {{
      const autoRun = !!options.autoRun;
      const input = document.getElementById('ocr-images');
      const status = document.getElementById('ocr-status');
      const button = document.getElementById('ocr-button');
      const runButton = document.getElementById('ocr-run-button');
      const panel = document.querySelector('.ocr-panel');
      if (panel) panel.open = true;
      if (!input.files || input.files.length === 0) {{
        status.textContent = 'Please choose at least one image, PDF, or Word file.';
        showToast('Choose a file first.', 'error');
        return;
      }}
      const formData = new FormData();
      for (const file of input.files) formData.append('images', file);
      formData.append('provider', document.getElementById('ocr-provider').value || 'paddleocr');
      formData.append('lang', document.getElementById('ocr-lang').value || 'chi_sim+eng');
      const targetMode = document.getElementById('ocr-target').value || 'preview';
      const activeButton = autoRun ? runButton : button;
      const oldText = activeButton ? activeButton.textContent : '';
      if (activeButton) {{
        activeButton.classList.add('is-busy');
        activeButton.textContent = autoRun ? 'OCR + Run...' : 'OCR...';
      }}
      if (button) button.disabled = true;
      if (runButton) runButton.disabled = true;
      status.textContent = 'Uploading file(s), submitting OCR job, and waiting for result...';
      startOcrProgress();
      try {{
        const response = await fetch(new URL('/ocr', window.location.origin).href, {{
          method: 'POST',
          body: formData
        }});
        const data = await response.json();
        if (!response.ok || !data.ok) throw new Error(data.message || ('HTTP ' + response.status));
        renderOcrResult(data);
        finishOcrProgress(true, 'OCR completed.');
        if (autoRun) {{
          if ((data.questions || []).length) {{
            applySelectedOcrQuestions('replace');
          }} else {{
            applyOcrResult('replace');
          }}
          status.textContent = (data.message || 'OCR completed.') + ' Starting pipeline...';
          showToast('OCR completed. Starting pipeline.');
          await runAction('pipeline');
        }} else if (targetMode === 'source') {{
          applyOcrResult('replace');
        }} else if (targetMode === 'append') {{
          applyOcrResult('append');
        }} else {{
          status.textContent = (data.message || 'OCR completed.') + ' Result is shown below. Apply it to Source when ready.';
          showToast('OCR completed. Preview the result below.');
        }}
      }} catch (error) {{
        finishOcrProgress(false, 'OCR failed.');
        status.textContent = 'OCR failed: ' + error.message;
        setErrorPanel('OCR failed: ' + error.message);
        showToast('OCR failed. See status line.', 'error');
      }} finally {{
        if (button) button.disabled = false;
        if (runButton) runButton.disabled = false;
        if (activeButton) {{
          activeButton.classList.remove('is-busy');
          activeButton.textContent = oldText;
        }}
      }}
    }}

    const liveLogElement = document.getElementById('run-live-log');
    liveLogElement.addEventListener('scroll', () => {{
      const distanceFromBottom = liveLogElement.scrollHeight - liveLogElement.scrollTop - liveLogElement.clientHeight;
      logUserPaused = distanceFromBottom > 24;
    }});
    liveLogElement.addEventListener('mousedown', () => {{ logUserPaused = true; }});
    liveLogElement.addEventListener('keydown', () => {{ logUserPaused = true; }});

    function fieldValue(name) {{
      const field = document.querySelector(`[name="${{name}}"]`);
      if (!field) return '';
      if (field.type === 'checkbox') return field.checked ? 'on' : '';
      return field.value;
    }}

    function collectPayload(action, actionOwner = '') {{
      const names = [
        'input', 'output_dir', 'review_threshold', 'max_review_rounds',
        'qc_threshold', 'max_qc_rounds', 'question_count_mode', 'question_count',
        'difficulty', 'knowledge_points', 'question_types', 'style', 'answer_detail',
        'allow_input_compaction', 'model_default', 'model_reader', 'model_decomposer',
        'model_reviewer', 'model_generator', 'model_qc',
        'thinking_reader', 'thinking_decomposer', 'thinking_reviewer', 'thinking_generator', 'thinking_qc',
        'effort_reader', 'effort_decomposer', 'effort_reviewer', 'effort_generator', 'effort_qc',
        'budget_total_warning', 'budget_total_stop',
        'budget_single_warning', 'budget_single_stop',
        'runtime_request_timeout_seconds', 'runtime_generator_warning_seconds',
        'runtime_generator_timeout_seconds', 'runtime_fallback_on_error',
        'runtime_fallback_on_timeout', 'result_store_enabled', 'result_store_dir'
      ];
      const payload = {{
        action,
        input_text: syncInputFromVisible(),
        engine_mode: engineMode,
        action_owner: actionOwner,
        selected_item_ids: selectedPipelineItemIds,
        confirm_pipeline: action === 'pipeline' ? 'yes' : ''
      }};
      for (const name of names) payload[name] = fieldValue(name);
      return payload;
    }}

    async function runAction(action, sourceButton = null, actionOwner = '') {{
      if (window.location.protocol !== 'http:' && window.location.protocol !== 'https:') {{
        showToast('Please open the Workbench from the local server URL, not as a file.', 'error');
        return;
      }}
      const messageText = action === 'pipeline' ? 'Pipeline starting...' : action === 'pause_pipeline' ? 'Pausing...' : action === 'resume_pipeline' ? 'Resuming...' : 'Working...';
      const button = sourceButton || document.querySelector(`button[onclick="runAction('${{action}}')"]`);
      const oldText = button ? button.textContent : '';
      let restoreTimer = null;
      const restoreButton = () => {{
        if (!button) return;
        button.classList.remove('is-busy');
        button.disabled = false;
        button.textContent = oldText;
      }};
      if (button) {{
        button.classList.add('is-busy');
        button.textContent = action === 'pipeline' ? 'Starting...' : action === 'pause_pipeline' ? 'Pausing...' : action === 'resume_pipeline' ? 'Resuming...' : 'Working...';
        button.disabled = true;
        restoreTimer = setTimeout(restoreButton, action === 'pipeline' ? 22000 : 18000);
      }}
      showToast(messageText);
      if (action === 'pipeline') {{
        clearOutputPanels();
      }}
      if (action === 'pipeline') {{
        document.getElementById('run-progress-text').textContent = '0%';
        document.getElementById('run-progress-fill').style.width = '0%';
        document.getElementById('run-item').textContent = '-/-';
        document.getElementById('current-step-label').textContent = 'Round - · Starting';
        document.getElementById('current-step-progress-text').textContent = '0% expected time';
        document.getElementById('current-step-progress-fill').style.width = '0%';
        for (const agent of ['reader', 'decomposer', 'reviewer', 'generator', 'qc']) {{
          const text = document.getElementById('agent-' + agent + '-text');
          const bar = document.getElementById('agent-' + agent + '-fill');
          if (text) text.textContent = '0%';
          if (bar) bar.style.width = '0%';
        }}
        setErrorPanel('');
        updateAgentFlow({{ running: false, stage: 'Idle', round: '-', agent_progress: {{ Reader: 0, Decomposer: 0, Reviewer: 0, Generator: 0, QC: 0 }} }});
        document.getElementById('run-live-log').textContent = 'Pipeline log will appear here after Run pipeline starts.';
      }}
      try {{
        const controller = new AbortController();
        const actionTimeouts = {{
          save_config: 15000,
          save_input: 15000,
          pipeline: 15000,
          stop_pipeline: 15000,
          pause_pipeline: 15000,
          resume_pipeline: 15000,
          show_options: 60000,
          preview: 60000,
          coverage: 240000,
          tests: 240000,
          bank_similar: 60000
        }};
        const timeoutId = setTimeout(() => controller.abort(), actionTimeouts[action] || 60000);
        const response = await fetch(new URL('/action', window.location.origin).href, {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify(collectPayload(action, actionOwner)),
          signal: controller.signal
        }});
        clearTimeout(timeoutId);
        if (!response.ok) {{
          throw new Error('HTTP ' + response.status);
        }}
        const data = await response.json();
        showToast(data.message || (data.ok ? 'Done.' : 'Failed.'), data.ok ? 'success' : 'error');
        setErrorPanel(data.ok ? '' : (data.message || 'Action failed.'));
        if (action === 'save_input') {{
          latestInputText = document.getElementById('input-source').value;
          if (inputMode === 'preview') renderInputPreview();
        }}
        if (action !== 'save_input') {{
          refreshRunStatus();
        }}
      }} catch (error) {{
        let detail = error.name === 'AbortError' ? 'request timed out' : error.message;
        let diagnosis = '';
        try {{
          const probeController = new AbortController();
          const probeTimeout = setTimeout(() => probeController.abort(), 3500);
          const health = await fetch(new URL('/status?probe=' + Date.now(), window.location.origin).href, {{ cache: 'no-store', signal: probeController.signal }});
          clearTimeout(probeTimeout);
          diagnosis = health.ok
            ? 'The UI server is reachable, but the /action request did not complete. The action handler may have crashed, timed out, or been interrupted.'
            : 'The UI server replied with HTTP ' + health.status + ' while checking /status.';
        }} catch (probeError) {{
          diagnosis = 'The local UI server is not reachable from this page. It may have stopped, the page may be using the wrong port, or the browser is opened from an old/stale address.';
        }}
        const message = 'Action failed: ' + detail + '\\n' + diagnosis;
        showToast('Action failed. See error panel.', 'error');
        setErrorPanel(message);
      }} finally {{
        if (restoreTimer) clearTimeout(restoreTimer);
        if (button) {{
          button.disabled = false;
          button.classList.remove('is-busy');
          button.textContent = action === 'save_input' ? 'Saved' : oldText;
          setTimeout(() => {{ button.textContent = oldText; }}, action === 'save_input' ? 1200 : 0);
        }}
      }}
    }}

    function showTab(id, btn) {{
      if (btn && btn.classList.contains('mode-hidden')) return;
      document.querySelectorAll('.tab').forEach(el => el.classList.remove('active'));
      document.querySelectorAll('.tab-button').forEach(el => el.classList.remove('active'));
      document.getElementById(id).classList.add('active');
      btn.classList.add('active');
      typesetMath([document.getElementById(id)]);
      scheduleAgentLinkUpdate();
    }}

    function typesetMath(elements) {{
      const targets = (elements || []).filter(Boolean);
      if (!window.MathJax || !window.MathJax.typesetPromise) return;
      const run = () => MathJax.typesetPromise(targets.length ? targets : undefined).catch(() => {{}});
      if (MathJax.startup && MathJax.startup.promise) {{
        MathJax.startup.promise.then(run).catch(() => {{}});
      }} else {{
        run();
      }}
    }}

    function updateLiveOutputPanels(liveOutputs) {{
      if (!liveOutputs) return;
      const questions = document.getElementById('questions-preview');
      const answers = document.getElementById('answers-preview');
      let changed = false;
      const questionsMtime = Number(liveOutputs.questions_mtime || 0);
      const answersMtime = Number(liveOutputs.answers_mtime || 0);
      if (outputResetAt && Math.max(questionsMtime, answersMtime) <= outputResetAt) {{
        return;
      }}
      if (!liveOutputs.questions_mtime && !liveOutputs.answers_mtime) {{
        const emptyPreview = '<p>No content yet.</p>';
        if (questions && questions.innerHTML !== emptyPreview) questions.innerHTML = emptyPreview;
        if (answers && answers.innerHTML !== emptyPreview) answers.innerHTML = emptyPreview;
        liveQuestionsMtime = 0;
        liveAnswersMtime = 0;
        return;
      }}
      if (questions && liveOutputs.questions && questionsMtime > outputResetAt && questionsMtime !== liveQuestionsMtime) {{
        liveQuestionsMtime = questionsMtime;
        questions.innerHTML = clientMarkdownToHtml(liveOutputs.questions);
        changed = true;
      }}
      if (answers && liveOutputs.answers && answersMtime > outputResetAt && answersMtime !== liveAnswersMtime) {{
        liveAnswersMtime = answersMtime;
        answers.innerHTML = clientMarkdownToHtml(liveOutputs.answers);
        changed = true;
      }}
      if (changed) typesetMath([questions, answers]);
    }}

    function clearOutputPanels(options = {{}}) {{
      const emptyPreview = '<p>No content yet.</p>';
      const keepTimeMachineActive = !!options.keepTimeMachineActive && document.getElementById('timemachine')?.classList.contains('active');
      const questions = document.getElementById('questions-preview');
      const answers = document.getElementById('answers-preview');
      const bankSimilar = document.getElementById('bank-similar-preview');
      const rendered = document.getElementById('render-preview');
      const qc = document.getElementById('qc');
      const coverage = document.getElementById('coverage');
      const debug = document.getElementById('debug');
      if (questions) questions.innerHTML = emptyPreview;
      if (answers) answers.innerHTML = emptyPreview;
      if (bankSimilar) bankSimilar.innerHTML = emptyPreview;
      outputResetAt = Date.now() / 1000;
      liveQuestionsMtime = 0;
      liveAnswersMtime = 0;
      if (rendered) rendered.innerHTML = emptyPreview;
      if (qc) qc.innerHTML = '<p>No QC result yet.</p>';
      if (coverage) coverage.innerHTML = '<pre>{{}}</pre>';
      if (debug) debug.innerHTML = '<pre>No debug files found.</pre>';
      if (!keepTimeMachineActive) {{
        document.querySelectorAll('.tab').forEach(el => el.classList.remove('active'));
        document.querySelectorAll('.tab-button').forEach(el => el.classList.remove('active'));
        document.getElementById('questions').classList.add('active');
        const firstButton = document.querySelector('.tab-button:not(.mode-hidden)');
        if (firstButton) firstButton.classList.add('active');
      }}
    }}

    async function refreshRunStatus() {{
      try {{
        const response = await fetch('/status');
        const data = await response.json();
        if (data.code_signature) {{
          document.getElementById('code-version').textContent = data.code_signature.version || '-';
          document.getElementById('code-latest').textContent = data.code_signature.latest_file || '-';
          if (data.code_time_machine) {{
            codeTimeMachine = data.code_time_machine;
            renderCodeTimeMachine();
          }}
          if (data.code_signature.version !== initialCodeSignature.version) {{
            if (data.running) {{
              pendingCodeReload = true;
            }} else {{
              showToast('Code changed. Refreshing UI...');
              setTimeout(() => window.location.reload(), 800);
              return;
            }}
          }}
        }}
        document.getElementById('run-stage').textContent = data.stage || 'Idle';
        document.getElementById('run-round').textContent = data.round || '-';
        const itemLabel = data.item_total ? ((data.item_index || '-') + '/' + data.item_total) : '-/-';
        document.getElementById('run-item').textContent = itemLabel;
        document.getElementById('run-progress-text').textContent = (data.progress || 0) + '%';
        const fill = document.getElementById('run-progress-fill');
        fill.style.width = (data.progress || 0) + '%';
        fill.classList.toggle('running', !!data.running && !data.paused);
        const stepPercent = data.step_progress || 0;
        const itemPrefix = data.item_total ? ('Item ' + itemLabel + ' · ') : '';
        document.getElementById('current-step-label').textContent = itemPrefix + 'Round ' + (data.round || '-') + ' · ' + (data.stage || 'Idle');
        document.getElementById('current-step-progress-text').textContent = stepPercent >= 100 ? 'expected time reached' : stepPercent + '% expected time';
        const currentStepFill = document.getElementById('current-step-progress-fill');
        currentStepFill.style.width = stepPercent + '%';
        currentStepFill.classList.toggle('running', !!data.running && !data.paused);
        const agentProgress = data.agent_progress || {{}};
        for (const agent of ['Reader', 'Decomposer', 'Reviewer', 'Generator', 'QC']) {{
          const key = agent.toLowerCase();
          const value = agentProgress[agent] || 0;
          const text = document.getElementById('agent-' + key + '-text');
          const bar = document.getElementById('agent-' + key + '-fill');
          if (text) text.textContent = value + '%';
          if (bar) {{
            bar.style.width = value + '%';
            bar.classList.toggle('running', !!data.running && !data.paused && data.stage === agent);
          }}
        }}
        updateAgentFlow(data);
        updateLiveOutputPanels(data.live_outputs);
        document.getElementById('step-label').textContent = 'Current step: ' + itemPrefix + 'Round ' + (data.round || '-') + ' · ' + (data.stage || 'Idle');
        document.getElementById('step-progress-text').textContent = stepPercent + '%';
        const stepFill = document.getElementById('step-progress-fill');
        stepFill.style.width = stepPercent + '%';
        stepFill.classList.toggle('running', !!data.running && !data.paused);
        document.getElementById('run-started').textContent = data.started_at || '-';
        document.getElementById('run-elapsed').textContent = data.elapsed || '-';
        const errorPanel = document.getElementById('error-panel');
        const errorText = document.getElementById('error-panel-text');
        if (data.error_message) {{
          if (errorText) errorText.textContent = data.error_message;
          errorPanel.classList.add('visible');
        }} else {{
          if (errorText) errorText.textContent = '';
          errorPanel.classList.remove('visible');
        }}
        const feedback = document.getElementById('stage-feedback');
        if (data.running && data.paused) {{
          feedback.textContent = 'Pipeline pause requested. It will wait before the next safe step; click Resume to continue.';
        }} else if (data.running && data.stage === 'Reader') {{
          feedback.textContent = 'Reader is preparing clean itemized input.';
        }} else if (data.running && data.stage === 'Reader item') {{
          feedback.textContent = 'Running item ' + (data.item_index || '-') + ' of ' + (data.item_total || '-') + '. Total progress follows completed items.';
        }} else if (data.running && data.stage === 'Generator') {{
          feedback.textContent = 'Generator is working. The progress bar is advancing by expected generation time.';
        }} else if (data.running && data.stage === 'QC') {{
          feedback.textContent = 'QC is checking the generated questions and answers. Progress is still active.';
        }} else if (data.running) {{
          feedback.textContent = data.stage + ' is running.';
        }} else {{
          feedback.textContent = '';
        }}
        const log = document.getElementById('run-live-log');
        const nextLogText = data.log || 'Pipeline log will appear here after Run pipeline starts.';
        const selection = window.getSelection ? window.getSelection() : null;
        const selectingLog = selection && selection.rangeCount > 0 && log.contains(selection.anchorNode);
        const nearBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 24;
        if (!selectingLog && (!logUserPaused || nearBottom || !data.running)) {{
          if (log.textContent !== nextLogText) {{
            log.textContent = nextLogText;
          }}
          if (data.running && (nearBottom || !logUserPaused)) {{
            log.scrollTop = log.scrollHeight;
            logUserPaused = false;
          }}
        }}
        if (data.running) {{
          lastRunning = true;
          reloadedAfterFinish = false;
        }} else if (lastRunning && !reloadedAfterFinish && (data.stage === 'Complete' || data.stage === 'Failed')) {{
          reloadedAfterFinish = true;
          setTimeout(() => window.location.reload(), pendingCodeReload ? 600 : 1200);
        }}
      }} catch (error) {{}}
    }}
    setInterval(refreshRunStatus, 1000);
    window.addEventListener('resize', scheduleAgentLinkUpdate);
    applyModes();
    renderCodeTimeMachine();
    renderQuestionBank();
    document.getElementById('bank-query')?.addEventListener('keydown', (event) => {{
      if (event.key === 'Enter') {{
        event.preventDefault();
        loadQuestionBank();
      }}
    }});
    document.getElementById('bank-tag')?.addEventListener('change', () => {{
      const selected = document.getElementById('bank-tag')?.value || '';
      document.querySelectorAll('.bank-tag-button').forEach(item => item.classList.toggle('active', item.dataset.tag === selected));
      loadQuestionBank();
    }});
    refreshRunStatus();
    scheduleAgentLinkUpdate();
    typesetMath([
      document.getElementById('questions-preview'),
      document.getElementById('answers-preview'),
      document.getElementById('bank-similar-preview'),
      document.getElementById('render-preview'),
      document.getElementById('input-preview')
    ]);
  </script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_common_headers("application/json; charset=utf-8", 0)
        self.end_headers()

    def do_GET(self):
        try:
            clean_path = self.path.split("?", 1)[0]
            if self.path.startswith("/status"):
                self.respond_json(get_live_run_state())
                return
            if self.path.startswith("/code-time-machine"):
                payload = code_time_machine_payload()
                payload["ok"] = True
                self.respond_json(payload)
                return
            if self.path.startswith("/question-bank"):
                options = load_options()
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                payload = load_question_bank_records(
                    options,
                    query=(query.get("query") or [""])[0],
                    tag=(query.get("tag") or [""])[0],
                    limit=(query.get("limit") or ["40"])[0],
                )
                self.respond_json(payload)
                return
            if self.path.startswith("/local-file"):
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                path = safe_project_file((query.get("path") or [""])[0])
                content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
                body = path.read_bytes()
                self.send_response(200)
                self.send_common_headers(content_type, len(body))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path.startswith("/replay"):
                options = load_options()
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                run_id = (query.get("id") or [""])[0]
                if run_id:
                    record = load_replay(
                        PROJECT_ROOT,
                        run_id,
                        output_dirs=[options.get("output_dir", "outputs"), "outputs", "outputs_professional"],
                        result_root=options.get("result_store", {}).get("dir", "result"),
                    )
                    self.respond_json({"ok": bool(record), "record": record})
                else:
                    self.respond_json(replay_payload(options))
                return
            if clean_path in {"", "/"}:
                self.send_response(302)
                self.send_header("Location", APP_PATH)
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return
            self.respond(render_page())
        except Exception as exc:
            self.respond_json({"ok": False, "message": f"GET failed: {exc}"}, status=500)

    def do_POST(self):
        try:
            if self.path.startswith("/code-time-machine"):
                length = int(self.headers.get("Content-Length", "0"))
                raw_body = self.rfile.read(length).decode("utf-8")
                form = json.loads(raw_body or "{}")
                action = form.get("action", "snapshot")
                if action == "restore":
                    restored = restore_code_snapshot(form.get("id", ""))
                    payload = code_time_machine_payload()
                    payload.update({"ok": True, "message": "Code snapshot restored.", "restored": restored})
                    self.respond_json(payload)
                    return
                snapshot = create_code_snapshot(reason="manual")
                payload = code_time_machine_payload()
                payload.update({"ok": True, "message": "Code snapshot saved.", "snapshot": snapshot})
                self.respond_json(payload)
                return
            if self.path.startswith("/ocr"):
                self.handle_ocr_upload()
                return

            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length).decode("utf-8")
            if self.path.startswith("/action"):
                form = json.loads(raw_body)
                if form.get("action") == "save_input":
                    input_path = project_path(form.get("input", load_options().get("input", "examples/input.md")))
                    write_text(input_path, normalize_input_text(form.get("input_text", "")))
                    update_run_state(error_message="")
                    self.respond_json(
                        {
                            "ok": True,
                            "message": f"Input saved: {rel_path(input_path)}",
                            "state": get_run_state(),
                        }
                    )
                    return
                message = self.handle_action(form)
                self.respond_json({"ok": True, "message": message, "state": get_live_run_state()})
                return

            form = {key: values[-1] for key, values in urllib.parse.parse_qs(raw_body).items()}
            message = self.handle_action(form)
            self.respond(render_page(message))
        except Exception as exc:
            message = f"Action failed: {exc}"
            update_run_state(error_message=message)
            self.respond_json({"ok": False, "message": message}, status=500)

    def handle_ocr_upload(self):
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length <= 0:
            self.respond_json({"ok": False, "message": "No OCR files were uploaded."}, status=400)
            return
        form = cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={
                "REQUEST_METHOD": "POST",
                "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                "CONTENT_LENGTH": str(content_length),
            },
        )
        fields = form["images"] if "images" in form else []
        if not isinstance(fields, list):
            fields = [fields]

        upload_dir = PROJECT_ROOT / "outputs" / "uploads" / f"ocr_{uuid4().hex[:10]}"
        upload_dir.mkdir(parents=True, exist_ok=True)
        saved_paths = []
        for index, field in enumerate(fields, start=1):
            if not getattr(field, "filename", ""):
                continue
            filename = safe_upload_filename(field, index)
            target = upload_dir / filename
            with target.open("wb") as file:
                shutil.copyfileobj(field.file, file)
            saved_paths.append(target)

        if not saved_paths:
            self.respond_json({"ok": False, "message": "No files were uploaded."}, status=400)
            return

        options = load_options()
        output_dir = project_path(options.get("output_dir", "outputs"))
        lang = form.getfirst("lang", "chi_sim+eng")
        provider = form.getfirst("provider", "paddleocr")
        word_paths = [path for path in saved_paths if is_word_path(path)]
        ocr_paths = [path for path in saved_paths if is_ocr_source_path(path)]
        unsupported_paths = [path for path in saved_paths if path not in word_paths and path not in ocr_paths]
        word_blocks = []
        for path in word_paths:
            try:
                text = extract_docx_text(path)
            except Exception as exc:
                self.respond_json({"ok": False, "message": f"Word import failed: {path.name}: {exc}"}, status=500)
                return
            if text:
                word_blocks.append(f"# {path.name}\n\n{text}")
        if unsupported_paths:
            self.respond_json(
                {
                    "ok": False,
                    "message": "Unsupported file type(s): " + ", ".join(path.name for path in unsupported_paths),
                },
                status=400,
            )
            return
        result = {
            "clean_output": str(output_dir / "ocr_clean.md"),
            "structured_output": str(output_dir / "ocr_structured.json"),
            "question_tree_output": str(output_dir / "ocr_question_tree.json"),
            "source_type": "word",
        }
        clean_text = "\n\n".join(word_blocks).strip()
        questions = []
        try:
            if ocr_paths:
                result = run_ocr(ocr_paths, output_dir, lang=lang, provider=provider)
                ocr_text = read_text(Path(result["clean_output"]))
                clean_text = "\n\n".join(part for part in [clean_text, ocr_text] if part.strip()).strip()
                if word_blocks:
                    write_text(Path(result["clean_output"]), clean_text)
                questions = ocr_question_options(result)
            elif clean_text:
                write_text(Path(result["clean_output"]), clean_text)
        except Exception as exc:
            message = clean_error_summary(str(exc), "ocr")
            update_run_state(error_message=f"OCR failed: {message}")
            self.respond_json({"ok": False, "message": f"OCR failed: {message}"}, status=500)
            return
        self.respond_json(
            {
                "ok": True,
                "message": f"File import completed: {len(saved_paths)} file(s).",
                "text": clean_text,
                "result": result,
                "uploaded": [
                    {
                        "path": rel_path(path),
                        "name": path.name,
                        "mime": mimetypes.guess_type(str(path))[0] or "",
                    }
                    for path in saved_paths
                ],
                "questions": questions,
            }
        )

    def handle_action(self, form):
        options = option_from_form(form)
        input_path = project_path(options["input"])
        action = form.get("action", "save_config")
        engine_mode = engine_mode_from_form(form)
        message = "Ready."

        if action in {"save_config", "save_input", "coverage", "show_options", "pipeline", "tests", "preview"}:
            saved_options = json.loads(json.dumps(options))
            saved_options["selected_item_ids"] = ""
            write_json(CONFIG_PATH, saved_options)
        if action == "pipeline" and form.get("confirm_pipeline") == "yes":
            clear_output_artifacts(project_path(options.get("output_dir", "outputs")))
        if action in {"save_input", "coverage", "pipeline"}:
            write_text(input_path, normalize_input_text(form.get("input_text", "")))

        try:
            if action == "save_config":
                message = "Configuration saved."
                update_run_state(error_message="")
            elif action == "save_input":
                message = f"Input saved: {rel_path(input_path)}"
                update_run_state(error_message="")
            elif action == "coverage":
                ok = run_command([sys.executable, "tests\\input_coverage_evaluator.py"], timeout=120)
                message = "Coverage updated." if ok else "Coverage command failed."
                if not ok:
                    update_run_state(error_message=command_error_message(message))
                else:
                    update_run_state(error_message="")
            elif action == "show_options":
                ok = run_command(pipeline_command(engine_mode, options) + ["--show-options"], timeout=60)
                message = "Effective options captured." if ok else "Show options failed."
                if not ok:
                    update_run_state(error_message=command_error_message(message))
                else:
                    update_run_state(error_message="")
            elif action == "pipeline":
                if form.get("confirm_pipeline") != "yes":
                    message = "Pipeline start was ignored because the request was not confirmed."
                    update_run_state(error_message="")
                    return message
                budget = options.get("token_budget", {})
                command = pipeline_command(engine_mode, options)
                started = start_background_command(command)
                if started:
                    append_run_log(f"UI engine mode: {engine_mode}\n")
                    append_run_log(
                        "UI token budget: "
                        f"total_warning={budget.get('total_warning')}, "
                        f"total_stop={budget.get('total_stop')}, "
                        f"single_warning={budget.get('single_warning')}, "
                        f"single_stop={budget.get('single_stop')}\n"
                    )
                message = (
                    f"{'Professional' if engine_mode == 'pro' else 'Flash'} pipeline started in the background."
                    if started
                    else "Pipeline is already running."
                )
            elif action == "stop_pipeline":
                stopped = stop_background_command()
                message = "Pipeline stopped." if stopped else "No running pipeline to stop."
            elif action == "pause_pipeline":
                paused = pause_background_command(form.get("action_owner", ""))
                message = "Pipeline pause requested." if paused else "No running pipeline to pause."
            elif action == "resume_pipeline":
                resumed = resume_background_command()
                message = "Pipeline resumed." if resumed else "No paused pipeline to resume."
            elif action == "tests":
                ok = run_command(["cmd", "/c", "run_local_tests.bat"], timeout=180)
                message = "Local tests passed." if ok else "Local tests failed."
                if not ok:
                    update_run_state(error_message=command_error_message(message))
                else:
                    update_run_state(error_message="")
            elif action == "preview":
                ok = run_command([sys.executable, "codes\\render_vscode_preview.py", "--output-dir", options["output_dir"]], timeout=60)
                message = "Preview rendered." if ok else "Preview render failed."
                if not ok:
                    update_run_state(error_message=command_error_message(message))
                else:
                    update_run_state(error_message="")
            elif action == "bank_similar":
                output_dir = project_path(options.get("output_dir", "outputs"))
                query_path = output_dir / "generated_questions_final.md"
                if not query_path.exists():
                    query_path = input_path
                ok = run_command(
                    [
                        sys.executable,
                        "codes\\question_bank.py",
                        "similar",
                        "--input",
                        str(query_path),
                        "--output-dir",
                        str(output_dir),
                        "--result-root",
                        str(project_path(options.get("result_store", {}).get("dir", "result"))),
                    ],
                    timeout=60,
                )
                message = "题库相似题已刷新。" if ok else "题库相似题检索失败。"
                if not ok:
                    update_run_state(error_message=command_error_message(message))
                else:
                    update_run_state(error_message="")
        except Exception as exc:
            message = f"Action failed: {exc}"
            update_run_state(error_message=message)

        return message

    def send_common_headers(self, content_type, content_length):
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", str(content_length))

    def respond(self, body, status=200):
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_common_headers("text/html; charset=utf-8", len(payload))
        self.end_headers()
        self.wfile.write(payload)

    def respond_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_common_headers("application/json; charset=utf-8", len(body))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print(fmt % args)


def main():
    port = PORT
    while True:
        try:
            server = ThreadingHTTPServer((HOST, port), Handler)
            break
        except OSError:
            port += 1
            if port > PORT + 20:
                raise
    url = f"http://{HOST}:{port}{APP_PATH}"
    launcher_path = PROJECT_ROOT / "open_ui.html"
    launcher_path.write_text(
        "\n".join(
            [
                "<!doctype html>",
                '<meta charset="utf-8">',
                f'<meta http-equiv="refresh" content="0; url={url}">',
                f'<title>Open Math Quiz Workbench</title>',
                f'<p>Opening <a href="{url}">{url}</a></p>',
            ]
        ),
        encoding="utf-8",
    )
    print(f"Math Quiz Workbench: {url}")
    print(f"Launcher page: {launcher_path}")
    webbrowser.open(url)
    server.serve_forever()


if __name__ == "__main__":
    main()
