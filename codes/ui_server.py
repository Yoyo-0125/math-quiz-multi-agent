import html
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent
if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))

from run_pipeline import DEFAULT_OPTIONS as PIPELINE_DEFAULT_OPTIONS


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "config" / "pipeline_options.json"
HOST = "127.0.0.1"
PORT = 8501
LAST_COMMAND = {"cmd": "", "code": "", "stdout": "", "stderr": ""}
RUN_LOCK = threading.Lock()
RUN_STATE = {
    "running": False,
    "stage": "Idle",
    "round": "-",
    "progress": 0,
    "step_progress": 0,
    "agent_progress": {
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
}

WATCHED_CODE_PATTERNS = ["codes/*.py", "tests/*.py", "*.bat"]


AGENT_ORDER = ["Decomposer", "Reviewer", "Generator", "QC"]
AGENT_NAME_MAP = {agent.lower(): agent for agent in AGENT_ORDER}
AGENT_EXPECTED_SECONDS = {
    "Decomposer": 45,
    "Reviewer": 35,
    "Generator": 260,
    "QC": 180,
}
TOTAL_EXPECTED_SECONDS = sum(AGENT_EXPECTED_SECONDS.values())
INCOMPLETE_STAGE_CAP = 97
STAGE_HEARTBEATS = {
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


def read_text(path, default=""):
    path = Path(path)
    if not path.exists():
        return default
    return path.read_text(encoding="utf-8")


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

    options["max_generated_questions"] = int(profile["question_count"])
    return options


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
        if RUN_STATE.get("running"):
            stage = RUN_STATE.get("stage", "Starting")
            stage_started = RUN_STATE.get("stage_started_ts") or RUN_STATE.get("started_ts") or now
            expected_seconds = AGENT_EXPECTED_SECONDS.get(stage, 30)
            elapsed_in_stage = max(0, now - stage_started)
            step_percent = expected_time_progress(elapsed_in_stage, expected_seconds)
            agent_progress = dict(RUN_STATE.get("agent_progress") or {})
            if stage in AGENT_ORDER:
                agent_progress[stage] = max(int(agent_progress.get(stage, 0)), step_percent)
            elapsed_total = max(0, now - (RUN_STATE.get("started_ts") or now))
            total_percent = expected_time_progress(elapsed_total, TOTAL_EXPECTED_SECONDS)
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
        state["code_signature"] = code_signature()
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
    lines = []
    for line in display_text(text).splitlines():
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
    stage = None

    running_match = re.search(
        r"===\s*Round\s+(\d+):\s+running\s+(Decomposer|Reviewer|Generator|QC)\s*===",
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
            changes["agent_progress"] = agent_progress
            changes["stage_started_ts"] = time.time()
            changes["last_heartbeat_at"] = 0
            changes["step_progress"] = 0
    if round_value:
        changes["round"] = round_value
    if changes:
        update_run_state(**changes)


def run_background(args):
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
                returncode=str(returncode),
                finished_at=time.strftime("%H:%M:%S"),
                finished_ts=time.time(),
            )
        else:
            failure = read_json(failure_path, {}) if recent_failure_file else {}
            error_text = failure.get("error") or failure.get("reason") or extract_error_summary(final_log, returncode)
            live_state = get_live_run_state()
            update_run_state(
                running=False,
                stage="Failed",
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


def start_background_command(args):
    if get_run_state().get("running"):
        return False
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
    )
    thread = threading.Thread(target=run_background, args=(args,), daemon=True)
    thread.start()
    return True


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
    preview_text = normalize_markdown_preview_text(text) or "No content yet."
    for raw in html.escape(preview_text).splitlines():
        line = raw.strip()
        if line.startswith("### "):
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


def render_page(message=""):
    options = load_options()
    profile = options["generation_profile"]
    models = options["models"]
    thinking = options["thinking"]
    effort = options["reasoning_effort"]
    budget = options["token_budget"]
    input_text = read_text(project_path(options["input"]))
    output_dir = project_path(options["output_dir"])
    coverage = read_json(PROJECT_ROOT / "docs" / "input_coverage_report.json", {})
    qc = read_json(output_dir / "qc_final.json")
    failure = read_json(output_dir / "pipeline_failed.json")
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

    qc_html = f"<pre>{esc(json.dumps(qc, ensure_ascii=False, indent=2))}</pre>" if qc else "<p>No QC result yet.</p>"
    if failure and not qc:
        qc_html = f'<p class="danger">{esc(failure.get("error") or failure.get("reason") or "Pipeline failed.")}</p><pre>{esc(json.dumps(failure, ensure_ascii=False, indent=2))}</pre>'

    coverage_cards = ""
    for key, label in [("line_coverage", "Line"), ("condition_coverage", "Condition"), ("branch_coverage", "Branch")]:
        data = coverage.get(key, {})
        coverage_cards += f'<div class="metric"><span>{label}</span><strong>{esc(data.get("covered", "-"))}/{esc(data.get("total", "-"))}</strong><em>{float(data.get("ratio", 0))*100:.1f}%</em></div>'
    input_text_json = json.dumps(input_text, ensure_ascii=False)
    code_signature_json = json.dumps(current_code_signature, ensure_ascii=False)
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
    .input-preview {{ min-height: 460px; max-height: 640px; overflow: auto; }}
    .input-preview.placeholder {{ color: #98a2b3; }}
    .hidden {{ display: none !important; }}
    button {{ border: 1px solid #0f766e; background: #0f766e; color: white; border-radius: 6px; padding: 9px 11px; font-weight: 700; cursor: pointer; }}
    button.secondary {{ background: #fff; color: #0f766e; }}
    .actions {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin: 10px 0 16px; }}
    .tabs {{ display: flex; gap: 6px; border-bottom: 1px solid #d8dde8; margin-bottom: 12px; flex-wrap: wrap; }}
    .tab-button {{ background: #fff; color: #344054; border: 1px solid #d8dde8; border-bottom: 0; padding: 8px 10px; }}
    .tab-button.active {{ color: #0f766e; background: #eefaf7; }}
    .tab {{ display: none; }}
    .tab.active {{ display: block; }}
    .preview {{ background: #fff; border: 1px solid #d8dde8; border-radius: 8px; padding: 10px; line-height: 1.1; overflow-x: auto; }}
    .preview p {{ margin: 0 0 1px; }}
    .preview h1, .preview h2, .preview h3 {{ margin: 4px 0 2px; line-height: 1.12; }}
    .preview ul, .preview ol {{ margin: 1px 0 2px 18px; padding: 0; }}
    .preview li {{ margin: 0; }}
    .preview br {{ display: none; }}
    pre {{ white-space: pre-wrap; word-break: break-word; background: #101828; color: #eef4ff; padding: 12px; border-radius: 8px; max-height: 520px; overflow: auto; }}
    .note {{ padding: 10px 12px; background: #eef4ff; border: 1px solid #c7d7fe; border-radius: 8px; margin-bottom: 14px; }}
    .message {{ padding: 10px 12px; background: #ecfdf3; border: 1px solid #abefc6; border-radius: 8px; margin-bottom: 14px; }}
    .danger {{ color: #b42318; }}
    .agent-row {{ display: grid; grid-template-columns: 1fr 110px; gap: 8px; align-items: center; }}
    .progress-wrap {{ border: 1px solid #d8dde8; background: #fff; border-radius: 8px; padding: 12px; margin-bottom: 14px; }}
    .progress-meta {{ display: grid; grid-template-columns: repeat(5, 1fr); gap: 10px; margin-bottom: 10px; }}
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
      white-space: pre-wrap;
      font-size: 13px;
    }}
    .error-panel.visible {{ display: block; }}
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
    @media (max-width: 1050px) {{ .header-row {{ display: block; }} .mode-controls {{ margin-top: 12px; justify-items: start; }} .wrap, .grid {{ grid-template-columns: 1fr; }} .status {{ grid-template-columns: 1fr 1fr; }} }}
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
          <div id="error-panel" class="error-panel">{esc(run_state.get("error_message", ""))}</div>
          <pre id="run-live-log" class="live-log">{esc(run_state.get("log", "") or "Pipeline log will appear here after Run pipeline starts.")}</pre>
        </div>
        <div class="grid">
          <section class="panel">
            <h2>Input</h2>
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
              <button type="button" class="tab-button" onclick="showTab('qc', this)">QC Result</button>
              <button type="button" data-admin-only class="tab-button" onclick="showTab('preview', this)">Preview</button>
              <button type="button" data-admin-only class="tab-button" onclick="showTab('coverage', this)">Coverage</button>
              <button type="button" data-admin-only class="tab-button" onclick="showTab('debug', this)">Debug</button>
            </div>
            <div id="questions" class="tab active"><div class="preview">{md_to_html(read_text(output_dir / "generated_questions_final.md"))}</div></div>
            <div id="answers" class="tab"><div class="preview">{md_to_html(read_text(output_dir / "answer_key_final.md"))}</div></div>
            <div id="qc" class="tab">{qc_html}</div>
            <div id="preview" data-admin-only class="tab"><div class="preview">{md_to_html(read_text(output_dir / "preview.md"))}</div></div>
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
    document.getElementById('server-origin').textContent = window.location.origin;
    let inputMode = 'preview';
    let latestInputText = initialInputText;
    const inputPlaceholder = 'Edit math Markdown / LaTeX in Source. Preview is read-only.';
    let lastRunning = false;
    let reloadedAfterFinish = false;
    let pendingCodeReload = false;
    let logUserPaused = false;
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

    function setWorkbenchMode(mode) {{
      workbenchMode = mode === 'user' ? 'user' : 'admin';
      localStorage.setItem('mathQuizWorkbenchMode', workbenchMode);
      applyModes();
      showToast(workbenchMode === 'user' ? '已切换到用户模式。' : '已切换到管理模式。');
    }}

    function setEngineMode(mode) {{
      engineMode = mode === 'pro' ? 'pro' : 'flash';
      localStorage.setItem('mathQuizEngineMode', engineMode);
      applyModes();
      showToast(engineMode === 'pro' ? '专业模式暂时沿用 flash 运行设置。' : '已切换到 flash 模式。');
    }}

    function setErrorPanel(text) {{
      const errorPanel = document.getElementById('error-panel');
      if (!errorPanel) return;
      const cleaned = cleanErrorText(text || '');
      if (cleaned) {{
        errorPanel.textContent = cleaned;
        errorPanel.classList.add('visible');
      }} else {{
        errorPanel.textContent = '';
        errorPanel.classList.remove('visible');
      }}
    }}

    function cleanErrorText(text) {{
      if (!text) return '';
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
      for (const raw of lines) {{
        const line = raw.trim();
        if (line.startsWith('### ')) {{
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
      if (window.MathJax) MathJax.typesetPromise([preview]);
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

    document.getElementById('input-source').addEventListener('input', (event) => {{
      latestInputText = event.target.value;
    }});

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

    function collectPayload(action) {{
      const names = [
        'input', 'output_dir', 'review_threshold', 'max_review_rounds',
        'qc_threshold', 'max_qc_rounds', 'question_count_mode', 'question_count',
        'difficulty', 'knowledge_points', 'question_types', 'style', 'answer_detail',
        'allow_input_compaction', 'model_default', 'model_reader', 'model_decomposer',
        'model_reviewer', 'model_generator', 'model_qc',
        'thinking_reader', 'thinking_decomposer', 'thinking_reviewer', 'thinking_generator', 'thinking_qc',
        'effort_reader', 'effort_decomposer', 'effort_reviewer', 'effort_generator', 'effort_qc',
        'budget_total_warning', 'budget_total_stop',
        'budget_single_warning', 'budget_single_stop'
      ];
      const payload = {{ action, input_text: syncInputFromVisible() }};
      for (const name of names) payload[name] = fieldValue(name);
      return payload;
    }}

    async function runAction(action) {{
      if (window.location.protocol !== 'http:' && window.location.protocol !== 'https:') {{
        showToast('Please open the Workbench from the local server URL, not as a file.', 'error');
        return;
      }}
      const messageText = action === 'pipeline' ? 'Pipeline starting...' : 'Working...';
      const button = document.querySelector(`button[onclick="runAction('${{action}}')"]`);
      const oldText = button ? button.textContent : '';
      if (button) {{
        button.classList.add('is-busy');
        button.textContent = action === 'pipeline' ? 'Starting...' : 'Working...';
      }}
      showToast(messageText);
      if (action === 'pipeline') {{
        document.getElementById('run-progress-text').textContent = '0%';
        document.getElementById('run-progress-fill').style.width = '0%';
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
        document.getElementById('run-live-log').textContent = 'Pipeline log will appear here after Run pipeline starts.';
      }}
      try {{
        const controller = new AbortController();
        const actionTimeouts = {{
          save_config: 15000,
          save_input: 15000,
          pipeline: 15000,
          show_options: 60000,
          preview: 60000,
          coverage: 240000,
          tests: 240000
        }};
        const timeoutId = setTimeout(() => controller.abort(), actionTimeouts[action] || 60000);
        const response = await fetch(new URL('/action', window.location.origin).href, {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify(collectPayload(action)),
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
        refreshRunStatus();
      }} catch (error) {{
        let detail = error.name === 'AbortError' ? 'request timed out' : error.message;
        let diagnosis = '';
        try {{
          const health = await fetch(new URL('/status?probe=' + Date.now(), window.location.origin).href, {{ cache: 'no-store' }});
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
        if (button) {{
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
      if (window.MathJax) MathJax.typesetPromise();
    }}
    async function refreshRunStatus() {{
      try {{
        const response = await fetch('/status');
        const data = await response.json();
        if (data.code_signature) {{
          document.getElementById('code-version').textContent = data.code_signature.version || '-';
          document.getElementById('code-latest').textContent = data.code_signature.latest_file || '-';
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
        document.getElementById('run-progress-text').textContent = (data.progress || 0) + '%';
        const fill = document.getElementById('run-progress-fill');
        fill.style.width = (data.progress || 0) + '%';
        fill.classList.toggle('running', !!data.running);
        const stepPercent = data.step_progress || 0;
        document.getElementById('current-step-label').textContent = 'Round ' + (data.round || '-') + ' · ' + (data.stage || 'Idle');
        document.getElementById('current-step-progress-text').textContent = stepPercent >= 100 ? 'expected time reached' : stepPercent + '% expected time';
        const currentStepFill = document.getElementById('current-step-progress-fill');
        currentStepFill.style.width = stepPercent + '%';
        currentStepFill.classList.toggle('running', !!data.running);
        const agentProgress = data.agent_progress || {{}};
        for (const agent of ['Decomposer', 'Reviewer', 'Generator', 'QC']) {{
          const key = agent.toLowerCase();
          const value = agentProgress[agent] || 0;
          const text = document.getElementById('agent-' + key + '-text');
          const bar = document.getElementById('agent-' + key + '-fill');
          if (text) text.textContent = value + '%';
          if (bar) {{
            bar.style.width = value + '%';
            bar.classList.toggle('running', !!data.running && data.stage === agent);
          }}
        }}
        document.getElementById('step-label').textContent = 'Current step: Round ' + (data.round || '-') + ' · ' + (data.stage || 'Idle');
        document.getElementById('step-progress-text').textContent = stepPercent + '%';
        const stepFill = document.getElementById('step-progress-fill');
        stepFill.style.width = stepPercent + '%';
        stepFill.classList.toggle('running', !!data.running);
        document.getElementById('run-started').textContent = data.started_at || '-';
        document.getElementById('run-elapsed').textContent = data.elapsed || '-';
        const errorPanel = document.getElementById('error-panel');
        if (data.error_message) {{
          errorPanel.textContent = data.error_message;
          errorPanel.classList.add('visible');
        }} else {{
          errorPanel.textContent = '';
          errorPanel.classList.remove('visible');
        }}
        const feedback = document.getElementById('stage-feedback');
        if (data.running && data.stage === 'Generator') {{
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
    applyModes();
    refreshRunStatus();
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
            if self.path.startswith("/status"):
                self.respond_json(get_live_run_state())
                return
            self.respond(render_page())
        except Exception as exc:
            self.respond_json({"ok": False, "message": f"GET failed: {exc}"}, status=500)

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length).decode("utf-8")
            if self.path.startswith("/action"):
                form = json.loads(raw_body)
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

    def handle_action(self, form):
        options = option_from_form(form)
        input_path = project_path(options["input"])
        action = form.get("action", "save_config")
        message = "Ready."

        if action in {"save_config", "save_input", "coverage", "show_options", "pipeline", "tests", "preview"}:
            write_json(CONFIG_PATH, options)
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
                ok = run_command([sys.executable, "codes\\run_pipeline.py", "--show-options"], timeout=60)
                message = "Effective options captured." if ok else "Show options failed."
                if not ok:
                    update_run_state(error_message=command_error_message(message))
                else:
                    update_run_state(error_message="")
            elif action == "pipeline":
                budget = options.get("token_budget", {})
                failure_path = project_path(options.get("output_dir", "outputs")) / "pipeline_failed.json"
                try:
                    if failure_path.exists():
                        failure_path.unlink()
                except OSError:
                    pass
                started = start_background_command(["python", "-u", "codes\\run_pipeline.py"])
                if started:
                    append_run_log(
                        "UI token budget: "
                        f"total_warning={budget.get('total_warning')}, "
                        f"total_stop={budget.get('total_stop')}, "
                        f"single_warning={budget.get('single_warning')}, "
                        f"single_stop={budget.get('single_stop')}\n"
                    )
                message = "Pipeline started in the background." if started else "Pipeline is already running."
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
    url = f"http://{HOST}:{port}"
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
