import json
import base64
import hashlib
import email.utils
import os
import re
import sys
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from common import write_json_file, write_text_file


SUPPORTED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}
SUPPORTED_OCR_EXTENSIONS = SUPPORTED_IMAGE_EXTENSIONS | {".pdf"}
TEXTIN_ENDPOINT = "https://api.textin.com/ai/service/v1/handwritten_erase"
YOUDAO_ENDPOINT = "https://openapi.youdao.com/ocr_writing_erase"
PADDLEOCR_DEFAULT_MODEL = "PaddleOCR-VL-1.6"
PADDLEOCR_DEFAULT_OPTIONAL_PAYLOAD = {
    "useDocOrientationClassify": False,
    "useDocUnwarping": False,
    "useChartRecognition": False,
}
TEXT_KEYS = {
    "text",
    "content",
    "words",
    "word",
    "value",
    "line_text",
    "recognized_text",
    "ocr_text",
}
IMAGE_KEYS = {
    "image",
    "eraseEnhanceImg",
    "erase_enhance_img",
    "processedImage",
    "processed_image",
}


class OcrProviderError(RuntimeError):
    def __init__(self, code, message, payload=None):
        super().__init__(message)
        self.code = code
        self.payload = sanitize_payload(payload or {})


def sanitize_payload(value):
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(secret in lowered for secret in ["token", "secret", "api_key", "authorization"]):
                cleaned[key] = "[redacted]"
            else:
                cleaned[key] = sanitize_payload(item)
        return cleaned
    if isinstance(value, list):
        return [sanitize_payload(item) for item in value]
    if isinstance(value, str):
        value = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+", "bearer [redacted]", value)
        value = re.sub(r"(?i)(token|api_key|secret)=([^&\s]+)", r"\1=[redacted]", value)
    return value


def normalize_ocr_text(text):
    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    normalized = re.sub(r"[ \t]+\n", "\n", normalized)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    normalized = normalized.strip()
    if not normalized:
        return ""
    return "\n".join(
        [
            "# OCR Input",
            "",
            "> This file is generated from uploaded image OCR. Please review it before running the pipeline.",
            "",
            normalized,
            "",
        ]
    )


def load_env_file():
    candidate_paths = [Path(__file__).resolve().parents[1] / ".env"]
    extra_files = os.getenv("OCR_ENV_FILES", "")
    for value in extra_files.split(os.pathsep):
        if value.strip():
            candidate_paths.append(Path(value.strip()).expanduser())
    for env_key in ["TEXTIN_ENV_FILE", "YOUDAO_ENV_FILE"]:
        if os.getenv(env_key):
            candidate_paths.append(Path(os.getenv(env_key, "")).expanduser())
    candidate_paths.extend(
        [
            Path.home() / ".config" / "marwyn" / "textin.env",
            Path.home() / ".config" / "marwyn" / "youdao.env",
        ]
    )

    for env_path in candidate_paths:
        if not env_path.exists():
            continue
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def parse_json_env(name, default):
    raw_value = os.getenv(name, "").strip()
    if not raw_value:
        return dict(default)
    try:
        parsed = json.loads(raw_value)
    except json.JSONDecodeError as error:
        raise OcrProviderError("bad_response", f"{name} is not valid JSON") from error
    if not isinstance(parsed, dict):
        raise OcrProviderError("bad_response", f"{name} must be a JSON object")
    merged = dict(default)
    merged.update(parsed)
    return merged


def env_int(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def env_float(name, default):
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def parse_simple_providers_yaml(path):
    path = Path(path)
    if not path.exists():
        return {}
    providers = {}
    current_provider = None
    in_providers = False
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if re.match(r"^providers\s*:\s*$", line):
            in_providers = True
            current_provider = None
            continue
        if not in_providers:
            continue
        provider_match = re.match(r"^\s{2}([A-Za-z0-9_-]+)\s*:\s*$", line)
        if provider_match:
            current_provider = provider_match.group(1)
            providers.setdefault(current_provider, {})
            continue
        field_match = re.match(r"^\s{4}([A-Za-z0-9_-]+)\s*:\s*(.*?)\s*$", line)
        if field_match and current_provider:
            key, value = field_match.group(1), field_match.group(2)
            value = value.strip().strip('"').strip("'")
            providers[current_provider][key] = value
    return providers


def resolve_config_secret(value):
    value = str(value or "").strip()
    if value.lower().startswith("env:"):
        return os.getenv(value.split(":", 1)[1].strip(), "").strip()
    return value


def load_paddleocr_provider_config():
    config_path = Path(__file__).resolve().parents[1] / "config" / "providers.yaml"
    providers = parse_simple_providers_yaml(config_path).get("paddleocr") or {}
    if not providers:
        providers = parse_simple_providers_yaml(config_path).get("ocr") or {}
    return providers


def load_paddleocr_config():
    load_env_file()
    provider_config = load_paddleocr_provider_config()
    endpoint = (
        provider_config.get("job_url")
        or provider_config.get("endpoint")
        or os.getenv("PADDLEOCR_JOB_URL")
        or os.getenv("PADDLEOCR_ENDPOINT")
        or os.getenv("PADDLEOCR_OCR_JOB_URL")
        or ""
    ).strip()
    token = resolve_config_secret(
        provider_config.get("token")
        or provider_config.get("api_key")
        or provider_config.get("token_ref")
        or os.getenv("PADDLEOCR_TOKEN")
        or os.getenv("PADDLEOCR_API_TOKEN")
        or ""
    )
    model = (provider_config.get("model") or os.getenv("PADDLEOCR_MODEL") or PADDLEOCR_DEFAULT_MODEL).strip()
    if not endpoint or not token:
        raise OcrProviderError(
            "missing_provider/endpoint/token",
            "PaddleOCR-VL needs config/providers.yaml providers.paddleocr.job_url plus token/token_ref, or PADDLEOCR_ENDPOINT and PADDLEOCR_TOKEN in .env",
            {"endpoint": endpoint, "token": token},
        )
    return {
        "endpoint": endpoint.rstrip("/"),
        "token": token,
        "model": model,
        "optional_payload": parse_json_env(
            "PADDLEOCR_OPTIONAL_PAYLOAD",
            PADDLEOCR_DEFAULT_OPTIONAL_PAYLOAD,
        ),
        "poll_initial_seconds": env_float("PADDLEOCR_POLL_INITIAL_SECONDS", 5),
        "poll_backoff": env_float("PADDLEOCR_POLL_BACKOFF", 1.5),
        "poll_max_seconds": env_float("PADDLEOCR_POLL_MAX_SECONDS", 20),
        "timeout_per_page_seconds": env_int("PADDLEOCR_TIMEOUT_PER_PAGE_SECONDS", 90),
        "timeout_min_seconds": env_int("PADDLEOCR_TIMEOUT_MIN_SECONDS", 120),
        "timeout_max_seconds": env_int("PADDLEOCR_TIMEOUT_MAX_SECONDS", 600),
        "submit_retries": env_int("PADDLEOCR_SUBMIT_RETRIES", 3),
        "fetch_retries": env_int("PADDLEOCR_FETCH_RETRIES", 4),
        "poll_unavailable_limit": env_int("PADDLEOCR_POLL_UNAVAILABLE_LIMIT", 5),
    }


def classify_http_error(error, response_text=""):
    status = getattr(error, "code", None)
    if status in [401, 403]:
        return "auth_or_config_failed"
    if status and 400 <= status < 500:
        return "remote_4xx"
    if status and status >= 500:
        return "poll_unavailable"
    return "network_error"


def read_http_text(request, timeout=90):
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as error:
        response_text = error.read().decode("utf-8", errors="replace")
        code = classify_http_error(error, response_text)
        raise OcrProviderError(
            code,
            f"OCR API HTTP {error.code}: {response_text[:300]}",
            {"status": error.code, "body": response_text[:800]},
        ) from error
    except urllib.error.URLError as error:
        raise OcrProviderError("network_error", f"OCR API network error: {error}") from error


def request_json(request, timeout=90):
    text = read_http_text(request, timeout=timeout)
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise OcrProviderError("bad_response", f"OCR API returned non-JSON response: {text[:500]}") from error


def multipart_body(fields, file_field, file_path):
    boundary = "----codex-ocr-" + uuid.uuid4().hex
    chunks = []
    for name, value in fields.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("utf-8"),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"),
                str(value).encode("utf-8"),
                b"\r\n",
            ]
        )
    path = Path(file_path)
    chunks.extend(
        [
            f"--{boundary}\r\n".encode("utf-8"),
            (
                f'Content-Disposition: form-data; name="{file_field}"; '
                f'filename="{path.name}"\r\n'
            ).encode("utf-8"),
            b"Content-Type: application/octet-stream\r\n\r\n",
            path.read_bytes(),
            b"\r\n",
            f"--{boundary}--\r\n".encode("utf-8"),
        ]
    )
    return boundary, b"".join(chunks)


def retry_operation(name, attempts, func):
    last_error = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            return func()
        except OcrProviderError as error:
            last_error = error
            if error.code in ["auth_or_config_failed", "remote_4xx"]:
                raise
            if attempt >= attempts:
                raise
            time.sleep(min(2 * attempt, 8))
    raise last_error or OcrProviderError("bad_response", f"{name} failed")


def paddleocr_headers(config):
    # PaddleOCR/Aistudio's gateway rejects requests without a current date header.
    stamp = email.utils.formatdate(time.time(), usegmt=True)
    return {
        "Authorization": f"bearer {config['token']}",
        "Date": stamp,
        "X-Bce-Date": stamp,
    }


def submit_paddleocr_job(source_path, config):
    source_text = str(source_path)
    headers = paddleocr_headers(config)

    def do_submit():
        if re.match(r"^https?://", source_text, flags=re.I):
            body = json.dumps(
                {
                    "fileUrl": source_text,
                    "model": config["model"],
                    "optionalPayload": config["optional_payload"],
                },
                ensure_ascii=False,
            ).encode("utf-8")
            request = urllib.request.Request(
                config["endpoint"],
                data=body,
                headers={**headers, "Content-Type": "application/json"},
                method="POST",
            )
        else:
            path = Path(source_path)
            if not path.exists():
                raise OcrProviderError("source_not_found", f"OCR source not found: {path}")
            boundary, body = multipart_body(
                {
                    "model": config["model"],
                    "optionalPayload": json.dumps(config["optional_payload"], ensure_ascii=False),
                },
                "file",
                path,
            )
            request = urllib.request.Request(
                config["endpoint"],
                data=body,
                headers={**headers, "Content-Type": f"multipart/form-data; boundary={boundary}"},
                method="POST",
            )
        payload = request_json(request, timeout=120)
        job_id = (payload.get("data") or {}).get("jobId") or payload.get("jobId")
        if not job_id:
            raise OcrProviderError("bad_response", "PaddleOCR submit response has no data.jobId", payload)
        return job_id, payload

    return retry_operation("submit", config["submit_retries"], do_submit)


def poll_paddleocr_job(job_id, config, page_hint=1):
    start = time.time()
    interval = max(float(config["poll_initial_seconds"]), 0.1)
    max_interval = max(float(config["poll_max_seconds"]), interval)
    timeout_seconds = min(
        max(config["timeout_per_page_seconds"] * max(page_hint, 1), config["timeout_min_seconds"]),
        config["timeout_max_seconds"],
    )
    unavailable_count = 0
    last_extracted_pages = -1
    headers = paddleocr_headers(config)
    job_url = f"{config['endpoint']}/{urllib.parse.quote(str(job_id))}"

    while True:
        if time.time() - start > timeout_seconds:
            raise OcrProviderError("timed_out_local", "PaddleOCR job timed out locally", {"jobId": job_id})
        try:
            request = urllib.request.Request(job_url, headers=headers, method="GET")
            payload = request_json(request, timeout=45)
            unavailable_count = 0
        except OcrProviderError as error:
            if error.code not in ["poll_unavailable", "network_error"]:
                raise
            unavailable_count += 1
            if unavailable_count >= config["poll_unavailable_limit"]:
                raise OcrProviderError(
                    "poll_unavailable_exhausted",
                    "PaddleOCR polling failed too many times",
                    {"jobId": job_id, "last_error": error.payload},
                ) from error
            time.sleep(interval)
            interval = min(interval * config["poll_backoff"], max_interval)
            continue

        data = payload.get("data") or payload
        state = str(data.get("state") or data.get("status") or "").lower()
        progress = data.get("extractProgress") or {}
        extracted_pages = int(progress.get("extractedPages") or progress.get("extracted_pages") or 0)
        total_pages = int(progress.get("totalPages") or progress.get("total_pages") or page_hint or 1)
        if extracted_pages > last_extracted_pages:
            last_extracted_pages = extracted_pages
            interval = max(float(config["poll_initial_seconds"]), 0.1)

        if state == "done":
            result_url = data.get("resultUrl") or {}
            json_url = result_url.get("jsonUrl") if isinstance(result_url, dict) else ""
            json_url = json_url or data.get("jsonUrl")
            if not json_url:
                raise OcrProviderError("missing_json_url", "PaddleOCR done response has no resultUrl.jsonUrl", payload)
            return json_url, payload, total_pages
        if state == "failed":
            raise OcrProviderError("job_failed", data.get("errorMsg") or "PaddleOCR job failed", payload)
        if state not in ["pending", "running", ""]:
            raise OcrProviderError("unknown_remote_state", f"Unknown PaddleOCR state: {state}", payload)

        time.sleep(interval)
        interval = min(interval * config["poll_backoff"], max_interval)


def fetch_paddleocr_jsonl(json_url, config):
    def do_fetch():
        # resultUrl.jsonUrl is a signed BOS URL; do not attach API Authorization headers.
        request = urllib.request.Request(json_url, method="GET")
        text = read_http_text(request, timeout=120)
        if not text.strip():
            raise OcrProviderError("bad_response", "PaddleOCR jsonl result is empty")
        return text

    return retry_operation("fetch", config["fetch_retries"], do_fetch)


def post_json_bytes(url, data, headers):
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            text = response.read().decode("utf-8", errors="replace")
    except Exception as error:
        raise RuntimeError(f"OCR API request failed: {error}") from error
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"OCR API returned non-JSON response: {text[:500]}") from error


def nested_get(payload, path):
    value = payload
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def first_present(payload, paths):
    for path in paths:
        value = nested_get(payload, path if isinstance(path, (list, tuple)) else [path])
        if value not in [None, ""]:
            return value
    return None


def normalize_bbox(value, page):
    if not value:
        return None
    if isinstance(value, (list, tuple)) and len(value) >= 4:
        x0, y0, x1, y1 = [float(item) for item in value[:4]]
        return {
            "page": page,
            "x": x0,
            "y": y0,
            "w": max(0.0, x1 - x0),
            "h": max(0.0, y1 - y0),
            "unit": "px",
            "origin": "top_left",
        }
    if isinstance(value, dict):
        if all(key in value for key in ["x", "y", "w", "h"]):
            x, y, w, h = value["x"], value["y"], value["w"], value["h"]
        elif all(key in value for key in ["left", "top", "width", "height"]):
            x, y, w, h = value["left"], value["top"], value["width"], value["height"]
        elif all(key in value for key in ["x0", "y0", "x1", "y1"]):
            x, y = value["x0"], value["y0"]
            w, h = float(value["x1"]) - float(x), float(value["y1"]) - float(y)
        else:
            return None
        return {
            "page": page,
            "x": float(x),
            "y": float(y),
            "w": max(0.0, float(w)),
            "h": max(0.0, float(h)),
            "unit": "px",
            "origin": "top_left",
        }
    return None


def block_text(block):
    return str(
        block.get("text")
        or block.get("content")
        or block.get("block_content")
        or block.get("value")
        or ""
    ).strip()


def block_kind(block):
    return str(
        block.get("kind")
        or block.get("type")
        or block.get("block_label")
        or block.get("label")
        or "text"
    ).lower()


def extract_blocks_from_page(page_payload, page_number):
    candidates = [
        ["layoutBlocks"],
        ["blocks"],
        ["layout", "blocks"],
        ["prunedResult", "blocks"],
        ["prunedResult", "parsing_res_list"],
        ["parsing_res_list"],
    ]
    raw_blocks = None
    for path in candidates:
        value = nested_get(page_payload, path)
        if isinstance(value, list):
            raw_blocks = value
            break
    markdown = first_present(page_payload, [["markdown", "text"], ["markdownText"], ["text"]]) or ""
    warnings = []
    blocks = []
    if not raw_blocks:
        if markdown.strip():
            blocks.append(
                {
                    "block_id": f"p{page_number:04d}-b0001",
                    "page": page_number,
                    "order": 1,
                    "kind": "text",
                    "text": markdown.strip(),
                    "bbox": None,
                    "bbox_status": "missing",
                }
            )
        return blocks, warnings

    for index, raw_block in enumerate(raw_blocks, start=1):
        if not isinstance(raw_block, dict):
            continue
        text = block_text(raw_block)
        if not text:
            continue
        bbox = normalize_bbox(
            raw_block.get("bbox")
            or raw_block.get("block_bbox")
            or raw_block.get("box")
            or raw_block.get("position"),
            page_number,
        )
        blocks.append(
            {
                "block_id": f"p{page_number:04d}-b{index:04d}",
                "page": page_number,
                "order": int(raw_block.get("order") or raw_block.get("index") or index),
                "kind": block_kind(raw_block),
                "text": text,
                "bbox": bbox,
                "bbox_status": "present" if bbox else "missing",
            }
        )
    return blocks, warnings


def infer_page_geometry(page_payload, blocks, warnings):
    width = first_present(
        page_payload,
        [["page_width"], ["pageWidth"], ["imageWidth"], ["width"], ["page", "width"]],
    )
    height = first_present(
        page_payload,
        [["page_height"], ["pageHeight"], ["imageHeight"], ["height"], ["page", "height"]],
    )
    if width and height:
        return float(width), float(height)
    boxes = [block["bbox"] for block in blocks if block.get("bbox")]
    if boxes:
        max_x = max(box["x"] + box["w"] for box in boxes)
        max_y = max(box["y"] + box["h"] for box in boxes)
        warnings.append("page_geometry_inferred_from_blocks")
        return max_x, max_y
    return None, None


def parse_paddleocr_jsonl(raw_jsonl, job_id="", model=PADDLEOCR_DEFAULT_MODEL):
    pages = []
    warnings = []
    page_number = 0
    for line_number, raw_line in enumerate(raw_jsonl.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as error:
            raise OcrProviderError("invalid_jsonl", f"Invalid PaddleOCR jsonl at line {line_number}") from error
        page_payloads = nested_get(payload, ["result", "layoutParsingResults"])
        if page_payloads is None:
            page_payloads = payload.get("layoutParsingResults") if isinstance(payload, dict) else None
        if page_payloads is None and isinstance(payload, dict):
            page_payloads = [payload]
        if not isinstance(page_payloads, list):
            raise OcrProviderError("bad_response", "PaddleOCR jsonl line has no layoutParsingResults", payload)
        for page_payload in page_payloads:
            if not isinstance(page_payload, dict):
                continue
            page_number += 1
            page_warnings = []
            blocks, block_warnings = extract_blocks_from_page(page_payload, page_number)
            page_warnings.extend(block_warnings)
            width, height = infer_page_geometry(page_payload, blocks, page_warnings)
            pages.append(
                {
                    "page": page_number,
                    "markdown": first_present(page_payload, [["markdown", "text"], ["markdownText"], ["text"]]) or "",
                    "markdown_images": first_present(page_payload, [["markdown", "images"]]) or {},
                    "output_images": page_payload.get("outputImages") or page_payload.get("output_images") or [],
                    "blocks": blocks,
                    "page_width": width,
                    "page_height": height,
                    "warnings": page_warnings,
                }
            )
    if not pages:
        warnings.append("no_pages_found")
    return {
        "job_id": job_id,
        "model": model,
        "pages": pages,
        "raw_jsonl": raw_jsonl,
        "warnings": warnings,
    }


CHINESE_NUMBER_VALUES = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
CIRCLED_DIGITS = "①②③④⑤⑥⑦⑧⑨⑩"


def parse_chinese_number(value):
    text = str(value).strip()
    if not text:
        return None
    if text in CHINESE_NUMBER_VALUES:
        return CHINESE_NUMBER_VALUES[text]
    if text == "十":
        return 10
    if "十" in text:
        left, right = text.split("十", 1)
        tens = CHINESE_NUMBER_VALUES.get(left, 1) if left else 1
        ones = CHINESE_NUMBER_VALUES.get(right, 0) if right else 0
        return tens * 10 + ones
    return None


def normalize_repeated_text(text):
    return re.sub(r"\s+", "", str(text or "")).strip()


def split_embedded_segments(block):
    text = block.get("text", "")
    lines = text.splitlines()
    if len(lines) <= 1:
        return [block]
    segments = []
    current = []
    recovered = False
    for line in lines:
        starts_new = bool(re.match(r"^\s*\d+[.．、]\s+\S", line)) and current
        if starts_new:
            new_block = dict(block)
            new_block["text"] = "\n".join(current).strip()
            segments.append(new_block)
            current = [line]
            recovered = True
        else:
            current.append(line)
    if current:
        new_block = dict(block)
        new_block["text"] = "\n".join(current).strip()
        if recovered:
            new_block["warnings"] = list(new_block.get("warnings", [])) + ["recovered_from_merged_block"]
        segments.append(new_block)
    return segments


def leading_label(text, minor_seen=False):
    stripped = str(text or "").lstrip()
    patterns = [
        ("subpart", re.compile(r"^([（(]\s*(\d+|[一二三四五六七八九十两]+)\s*[）)])")),
        ("subpart", re.compile(r"^([①②③④⑤⑥⑦⑧⑨⑩])")),
        ("major", re.compile(r"^([一二三四五六七八九十两]+)\s*[、.．]\s*")),
        ("major", re.compile(r"^(第\s*([一二三四五六七八九十两\d]+)\s*题)\s*")),
        ("minor", re.compile(r"^(\d+)\s*[.．、]\s+")),
        ("minor", re.compile(r"^(第\s*(\d+)\s*题)\s*")),
    ]
    for level, pattern in patterns:
        match = pattern.match(stripped)
        if not match:
            continue
        if level == "major" and minor_seen:
            continue
        raw = match.group(1)
        number_text = match.group(2) if match.lastindex and match.lastindex >= 2 else raw
        if raw in CIRCLED_DIGITS:
            number = CIRCLED_DIGITS.index(raw) + 1
        elif re.search(r"\d+", number_text):
            number = int(re.search(r"\d+", number_text).group(0))
        else:
            number = parse_chinese_number(re.sub(r"[第题\s、.．]", "", number_text))
        return {
            "level": level,
            "raw": raw,
            "number": number,
            "label_norm": f"{level}:{number if number is not None else raw}",
            "title_text": stripped[match.end() :].strip() or stripped,
        }
    return None


def score_label(label, block):
    score = 0.86
    if not block.get("bbox"):
        score -= 0.16
    if block.get("kind") == "table":
        score -= 0.5
    if label["level"] == "subpart" and not re.match(r"^\s*([（(]|[①②③④⑤⑥⑦⑧⑨⑩])", block.get("text", "")):
        score -= 0.2
    return round(score, 2)


def flatten_ocr_blocks(pages):
    blocks = []
    for page in pages:
        blocks.extend(page.get("blocks", []))
    blocks.sort(key=lambda item: (item.get("page", 0), item.get("order", 0)))
    counts = {}
    for block in blocks:
        normalized = normalize_repeated_text(block.get("text", ""))
        if normalized:
            counts[normalized] = counts.get(normalized, 0) + 1
    filtered = []
    for block in blocks:
        bbox = block.get("bbox") or {}
        y = float(bbox.get("y", 9999)) if bbox else 9999
        normalized = normalize_repeated_text(block.get("text", ""))
        if normalized and counts.get(normalized, 0) > 1 and (y < 40 or y > 1800):
            continue
        for segment in split_embedded_segments(block):
            filtered.append(segment)
    return filtered


def build_question_tree(pages, question_id="ocr-question-tree"):
    blocks = flatten_ocr_blocks(pages)
    nodes = []
    ambiguous = []
    stack = []
    minor_seen = False
    level_rank = {"major": 1, "minor": 2, "subpart": 3}
    for block in blocks:
        label = leading_label(block.get("text", ""), minor_seen=minor_seen)
        if not label:
            continue
        if label["level"] == "minor":
            minor_seen = True
        confidence = score_label(label, block)
        if confidence < 0.5:
            ambiguous.append({"label": label["raw"], "block_id": block.get("block_id"), "reason": "low_score"})
            continue
        rank = level_rank[label["level"]]
        while stack and level_rank[stack[-1]["level"]] >= rank:
            stack.pop()
        path = [item["label_number"] for item in stack] + [label["number"] or len(nodes) + 1]
        node = {
            "node_id": "q-" + "-".join(str(item) for item in path),
            "level": label["level"],
            "label_raw": label["raw"],
            "label_norm": label["label_norm"],
            "label_number": label["number"] or len(nodes) + 1,
            "path": path,
            "title_text": label["title_text"],
            "start": {
                "page": block.get("page"),
                "block_id": block.get("block_id"),
                "order": block.get("order"),
            },
            "end": None,
            "bbox_pages": [],
            "children": [],
            "confidence": confidence,
            "warnings": list(block.get("warnings", [])),
            "_block": block,
        }
        if stack:
            stack[-1]["children"].append(node["node_id"])
        nodes.append(node)
        stack.append(node)

    assign_question_ranges(nodes, blocks)
    assign_bbox_pages(nodes, blocks)
    for node in nodes:
        node.pop("_block", None)
        node.pop("label_number", None)
    nodes.sort(key=lambda item: (len(item["path"]), item["path"], item["start"].get("page") or 0, item["start"].get("order") or 0))
    return {
        "question_id": question_id,
        "source_pages": [page.get("page") for page in pages],
        "pages": [
            {
                "page": page.get("page"),
                "page_width": page.get("page_width"),
                "page_height": page.get("page_height"),
                "warnings": page.get("warnings", []),
            }
            for page in pages
        ],
        "nodes": nodes,
        "ambiguous": ambiguous,
    }


def block_position_key(block):
    return (block.get("page", 0), block.get("order", 0), block.get("block_id", ""))


def assign_question_ranges(nodes, blocks):
    sorted_blocks = sorted(blocks, key=block_position_key)
    sorted_nodes = sorted(nodes, key=lambda node: block_position_key(node["_block"]))
    for index, node in enumerate(sorted_nodes):
        node_rank = {"major": 1, "minor": 2, "subpart": 3}[node["level"]]
        next_node = None
        for candidate in sorted_nodes[index + 1 :]:
            candidate_rank = {"major": 1, "minor": 2, "subpart": 3}[candidate["level"]]
            if candidate_rank <= node_rank:
                next_node = candidate
                break
        start_key = block_position_key(node["_block"])
        end_block = sorted_blocks[-1] if sorted_blocks else node["_block"]
        if next_node:
            next_key = block_position_key(next_node["_block"])
            before = [block for block in sorted_blocks if start_key <= block_position_key(block) < next_key]
            if before:
                end_block = before[-1]
        node["end"] = {
            "page": end_block.get("page"),
            "block_id": end_block.get("block_id"),
            "order": end_block.get("order"),
        }


def merge_boxes(boxes):
    if not boxes:
        return []
    boxes = sorted(boxes, key=lambda box: (box["y"], box["x"]))
    merged = []
    current = dict(boxes[0])
    for box in boxes[1:]:
        gap = box["y"] - (current["y"] + current["h"])
        overlaps_x = box["x"] <= current["x"] + current["w"] and current["x"] <= box["x"] + box["w"]
        if gap <= 4 and overlaps_x:
            x0 = min(current["x"], box["x"])
            y0 = min(current["y"], box["y"])
            x1 = max(current["x"] + current["w"], box["x"] + box["w"])
            y1 = max(current["y"] + current["h"], box["y"] + box["h"])
            current.update({"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0})
        else:
            merged.append(current)
            current = dict(box)
    merged.append(current)
    return merged


def assign_bbox_pages(nodes, blocks):
    sorted_blocks = sorted(blocks, key=block_position_key)
    for node in nodes:
        start = (node["start"].get("page"), node["start"].get("order"), node["start"].get("block_id"))
        end = (node["end"].get("page"), node["end"].get("order"), node["end"].get("block_id"))
        node_blocks = [block for block in sorted_blocks if start <= block_position_key(block) <= end]
        grouped = {}
        for block in node_blocks:
            if block.get("bbox"):
                grouped.setdefault(block["page"], []).append(block["bbox"])
        if not grouped:
            node["warnings"].append("bbox_missing_on_page")
            continue
        node["bbox_pages"] = [
            {"page": page, "boxes": merge_boxes(boxes)}
            for page, boxes in sorted(grouped.items())
        ]


def question_tree_to_markdown(tree, pages):
    if tree.get("nodes"):
        lines = ["# OCR Input", ""]
        for node in tree["nodes"]:
            indent = "  " * max(len(node.get("path", [])) - 1, 0)
            label = "- " if indent else ""
            lines.append(f"{indent}{label}{node['label_raw']} {node['title_text']}".rstrip())
        return "\n".join(lines).strip() + "\n"
    page_text = "\n\n".join(page.get("markdown", "").strip() for page in pages if page.get("markdown", "").strip())
    return normalize_ocr_text(page_text)


def locate_question(question_tree, selector):
    numbers = [int(value) for value in re.findall(r"\d+", str(selector or ""))]
    if not numbers:
        return {"status": "not_found", "selector": selector}
    matches = [node for node in question_tree.get("nodes", []) if node.get("path") == numbers]
    if len(matches) == 1:
        return {"status": "matched", "node": matches[0], "confidence": 1.0}
    if len(matches) > 1:
        return {"status": "ambiguous", "matches": matches}
    return {"status": "not_found", "selector": selector}


def bbox_iou(a, b):
    ax1, ay1, ax2, ay2 = a["x"], a["y"], a["x"] + a["w"], a["y"] + a["h"]
    bx1, by1, bx2, by2 = b["x"], b["y"], b["x"] + b["w"], b["y"] + b["h"]
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
    union = a["w"] * a["h"] + b["w"] * b["h"] - inter
    return inter / union if union else 0


def locate_question_region(question_tree, region_bbox, threshold=0.05):
    best = None
    best_score = 0
    for node in question_tree.get("nodes", []):
        for page_entry in node.get("bbox_pages", []):
            if page_entry.get("page") != region_bbox.get("page"):
                continue
            for box in page_entry.get("boxes", []):
                score = bbox_iou(box, region_bbox)
                if score > best_score:
                    best = node
                    best_score = score
    if not best or best_score < threshold:
        return {"status": "region_match_below_threshold", "score": best_score}
    return {"status": "matched", "node": best, "score": best_score}


def extract_text_from_json(payload):
    parts = []

    def visit(value, parent_key=""):
        if isinstance(value, dict):
            for key, item in value.items():
                normalized_key = str(key).lower()
                if normalized_key in TEXT_KEYS and isinstance(item, str):
                    stripped = item.strip()
                    if stripped:
                        parts.append(stripped)
                else:
                    visit(item, normalized_key)
        elif isinstance(value, list):
            for item in value:
                visit(item, parent_key)
        elif isinstance(value, str) and parent_key in TEXT_KEYS:
            stripped = value.strip()
            if stripped:
                parts.append(stripped)

    visit(payload)
    unique_parts = []
    seen = set()
    for part in parts:
        if part in seen:
            continue
        seen.add(part)
        unique_parts.append(part)
    return "\n".join(unique_parts).strip()


def extract_base64_image_from_json(payload):
    def visit(value, parent_key=""):
        if isinstance(value, dict):
            for key, item in value.items():
                normalized_key = str(key)
                if normalized_key in IMAGE_KEYS and isinstance(item, str) and item.strip():
                    return item.strip()
                found = visit(item, normalized_key)
                if found:
                    return found
        elif isinstance(value, list):
            for item in value:
                found = visit(item, parent_key)
                if found:
                    return found
        return None

    return visit(payload)


def decode_base64_image(value):
    if not value:
        raise RuntimeError("OCR erase API returned empty image data")
    if "," in value and value.split(",", 1)[0].lower().startswith("data:"):
        value = value.split(",", 1)[1]
    return base64.b64decode(value)


def save_processed_image(image_bytes, output_dir, source_path, suffix="erased"):
    image_dir = Path(output_dir) / "ocr_processed_images"
    image_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(source_path).stem
    target = image_dir / f"{stem}_{suffix}.jpg"
    target.write_bytes(image_bytes)
    return target


def erase_textin_image(path, output_dir):
    load_env_file()
    app_id = os.getenv("TEXTIN_APP_ID", "")
    secret_code = os.getenv("TEXTIN_SECRET_CODE", "")
    endpoint = os.getenv("TEXTIN_OCR_ENDPOINT", TEXTIN_ENDPOINT)
    if not app_id or not secret_code:
        raise RuntimeError("TextIn OCR needs TEXTIN_APP_ID and TEXTIN_SECRET_CODE in .env")

    payload = Path(path).read_bytes()
    result = post_json_bytes(
        endpoint,
        payload,
        {
            "Content-Type": "application/octet-stream",
            "x-ti-app-id": app_id,
            "x-ti-secret-code": secret_code,
        },
    )
    if result.get("code") not in [200, "200", None]:
        raise RuntimeError(f"TextIn erase failed: {json.dumps(result, ensure_ascii=False)[:800]}")
    image_value = extract_base64_image_from_json(result)
    if not image_value:
        raise RuntimeError(f"TextIn erase response did not contain result.image: {json.dumps(result, ensure_ascii=False)[:800]}")
    processed_path = save_processed_image(
        decode_base64_image(image_value),
        output_dir,
        path,
        suffix="textin_erased",
    )
    return processed_path, result


def truncate_for_youdao_sign(value):
    if len(value) <= 20:
        return value
    return value[:10] + str(len(value)) + value[-10:]


def erase_youdao_image(path, output_dir):
    load_env_file()
    app_key = os.getenv("YOUDAO_APP_KEY", "")
    app_secret = os.getenv("YOUDAO_APP_SECRET", "")
    endpoint = os.getenv("YOUDAO_OCR_ENDPOINT", YOUDAO_ENDPOINT)
    image_field = os.getenv("YOUDAO_IMAGE_FIELD", "q")
    if not app_key or not app_secret:
        raise RuntimeError("Youdao OCR needs YOUDAO_APP_KEY and YOUDAO_APP_SECRET in .env")

    image_base64 = base64.b64encode(Path(path).read_bytes()).decode("ascii")
    salt = str(int(time.time() * 1000))
    curtime = str(int(time.time()))
    sign_text = app_key + truncate_for_youdao_sign(image_base64) + salt + curtime + app_secret
    data = {
        "appKey": app_key,
        image_field: image_base64,
        "salt": salt,
        "curtime": curtime,
        "signType": "v3",
        "sign": hashlib.sha256(sign_text.encode("utf-8")).hexdigest(),
    }
    extra = os.getenv("YOUDAO_OCR_EXTRA_PARAMS", "")
    if extra:
        data.update(dict(urllib.parse.parse_qsl(extra, keep_blank_values=True)))

    encoded = urllib.parse.urlencode(data).encode("utf-8")
    result = post_json_bytes(
        endpoint,
        encoded,
        {"Content-Type": "application/x-www-form-urlencoded"},
    )
    error_code = str(result.get("errorCode", "0"))
    if error_code not in ["0", "None", ""]:
        raise RuntimeError(f"Youdao erase failed: {json.dumps(result, ensure_ascii=False)[:800]}")
    image_value = extract_base64_image_from_json(result)
    if not image_value:
        raise RuntimeError(f"Youdao erase response did not contain eraseEnhanceImg: {json.dumps(result, ensure_ascii=False)[:800]}")
    processed_path = save_processed_image(
        decode_base64_image(image_value),
        output_dir,
        path,
        suffix="youdao_erased",
    )
    return processed_path, result


def load_tesseract_backend():
    try:
        from PIL import Image
    except ImportError as error:
        raise RuntimeError(
            "OCR needs Pillow. Install it with: pip install pillow pytesseract"
        ) from error

    try:
        import pytesseract
    except ImportError as error:
        raise RuntimeError(
            "OCR needs pytesseract. Install it with: pip install pillow pytesseract"
        ) from error

    return Image, pytesseract


def recognize_tesseract_image(path, lang):
    Image, pytesseract = load_tesseract_backend()
    with Image.open(path) as image:
        text = pytesseract.image_to_string(image, lang=lang)
    return text, {"backend": "tesseract"}


def recognize_one_image(path, provider, lang, output_dir):
    provider = (provider or "textin").lower()
    if provider == "textin":
        processed_path, api_result = erase_textin_image(path, output_dir)
        text, tesseract_result = recognize_tesseract_image(processed_path, lang)
        return text, {
            "provider": "textin",
            "erase_response": api_result,
            "processed_image": str(processed_path),
            "ocr_response": tesseract_result,
        }
    if provider == "youdao":
        processed_path, api_result = erase_youdao_image(path, output_dir)
        text, tesseract_result = recognize_tesseract_image(processed_path, lang)
        return text, {
            "provider": "youdao",
            "erase_response": api_result,
            "processed_image": str(processed_path),
            "ocr_response": tesseract_result,
        }
    if provider == "tesseract":
        return recognize_tesseract_image(path, lang)
    if provider == "textin_erase":
        processed_path, api_result = erase_textin_image(path, output_dir)
        return f"[TextIn erased image saved: {processed_path}]", {
            "provider": "textin_erase",
            "erase_response": api_result,
            "processed_image": str(processed_path),
        }
    if provider == "youdao_erase":
        processed_path, api_result = erase_youdao_image(path, output_dir)
        return f"[Youdao erased image saved: {processed_path}]", {
            "provider": "youdao_erase",
            "erase_response": api_result,
            "processed_image": str(processed_path),
        }
    raise ValueError(f"Unsupported OCR provider: {provider}")


def run_paddleocr_sources(source_paths, output_dir):
    config = load_paddleocr_config()
    structured_results = []
    api_results = []
    combined_pages = []
    combined_jsonl = []
    warnings = []
    page_offset = 0
    for source_path in source_paths:
        path = Path(source_path)
        if not re.match(r"^https?://", str(source_path), flags=re.I):
            if not path.exists():
                raise OcrProviderError("source_not_found", f"OCR source not found: {path}")
            if path.suffix.lower() not in SUPPORTED_OCR_EXTENSIONS:
                warnings.append(f"Skipped unsupported OCR source type: {path.name}")
                continue
        job_id, submit_payload = submit_paddleocr_job(source_path, config)
        json_url, poll_payload, total_pages = poll_paddleocr_job(job_id, config, page_hint=1)
        raw_jsonl = fetch_paddleocr_jsonl(json_url, config)
        parsed = parse_paddleocr_jsonl(raw_jsonl, job_id=job_id, model=config["model"])
        for page in parsed["pages"]:
            page_offset += 1
            original_page = page["page"]
            page["page"] = page_offset
            for block in page.get("blocks", []):
                block["page"] = page_offset
                if block.get("bbox"):
                    block["bbox"]["page"] = page_offset
                block["block_id"] = re.sub(r"^p\d{4}", f"p{page_offset:04d}", block["block_id"])
            page["source_page"] = original_page
            page["source"] = str(source_path)
            combined_pages.append(page)
        structured_results.append(parsed)
        combined_jsonl.append(raw_jsonl)
        warnings.extend(parsed.get("warnings", []))
        api_results.append(
            {
                "source": str(source_path),
                "provider": "paddleocr",
                "job_id": job_id,
                "submit": sanitize_payload(submit_payload),
                "poll": sanitize_payload(poll_payload),
                "json_url": sanitize_payload(json_url),
                "total_pages": total_pages,
            }
        )

    structured = {
        "job_id": ",".join(item.get("job_id", "") for item in structured_results if item.get("job_id")),
        "model": config["model"],
        "pages": combined_pages,
        "raw_jsonl": "\n".join(combined_jsonl),
        "warnings": warnings,
    }
    question_tree = build_question_tree(combined_pages)
    clean_text = question_tree_to_markdown(question_tree, combined_pages)
    raw_text = "\n\n".join(
        page.get("markdown", "").strip()
        or "\n".join(block.get("text", "") for block in page.get("blocks", []))
        for page in combined_pages
    ).strip()
    return raw_text, clean_text, structured, question_tree, warnings, api_results


def recognize_images(image_paths, output_dir, lang="chi_sim+eng", provider="textin"):
    chunks = []
    warnings = []
    api_results = []

    for index, image_path in enumerate(image_paths, start=1):
        path = Path(image_path)
        if path.suffix.lower() not in SUPPORTED_IMAGE_EXTENSIONS:
            warnings.append(f"Skipped unsupported image type: {path.name}")
            continue
        try:
            text, api_result = recognize_one_image(path, provider, lang, output_dir)
        except Exception as error:
            raise RuntimeError(
                f"OCR failed with provider={provider}. Detail: {error}"
            ) from error

        chunks.append(f"## Image {index}: {path.name}\n\n{text.strip()}")
        api_results.append({"image": str(path), "provider": provider, "response": api_result})

    return "\n\n".join(chunks).strip(), warnings, api_results


def run_ocr(image_paths, output_dir, lang="chi_sim+eng", provider="textin"):
    output_dir = Path(output_dir)
    image_paths = [Path(path) for path in image_paths]
    if not image_paths:
        raise ValueError("No images were provided for OCR")

    provider = (provider or "textin").lower()
    structured = None
    question_tree = None
    try:
        if provider in ["paddleocr", "paddleocr_vl", "paddleocr-vl"]:
            raw_text, clean_text, structured, question_tree, warnings, api_results = run_paddleocr_sources(
                image_paths,
                output_dir,
            )
            provider = "paddleocr"
        else:
            raw_text, warnings, api_results = recognize_images(
                image_paths,
                output_dir,
                lang=lang,
                provider=provider,
            )
            clean_text = normalize_ocr_text(raw_text)
    except OcrProviderError as error:
        raise RuntimeError(
            f"OCR failed with code={error.code}. Detail: {error}. Payload: "
            f"{json.dumps(error.payload, ensure_ascii=False)[:600]}"
        ) from error

    if not clean_text:
        warnings.append("OCR returned empty text. The image may be too blurry or unsupported.")

    result = {
        "image_count": len(image_paths),
        "images": [str(path) for path in image_paths],
        "provider": provider,
        "lang": lang,
        "raw_output": str(output_dir / "ocr_raw.md"),
        "clean_output": str(output_dir / "ocr_clean.md"),
        "api_output": str(output_dir / "ocr_api_result.json"),
        "structured_output": str(output_dir / "ocr_structured.json") if structured else "",
        "question_tree_output": str(output_dir / "ocr_question_tree.json") if question_tree else "",
        "warnings": warnings,
    }

    write_text_file(output_dir / "ocr_raw.md", raw_text)
    write_text_file(output_dir / "ocr_clean.md", clean_text)
    write_json_file(output_dir / "ocr_result.json", result)
    write_json_file(output_dir / "ocr_api_result.json", api_results)
    if structured:
        write_json_file(output_dir / "ocr_structured.json", structured)
        write_text_file(output_dir / "ocr_raw.jsonl", structured.get("raw_jsonl", ""))
    if question_tree:
        write_json_file(output_dir / "ocr_question_tree.json", question_tree)
    return result


def main():
    if len(sys.argv) < 3:
        print("Usage: python ocr_agent.py <output_dir> <image1> [image2 ...]")
        raise SystemExit(1)
    provider = os.getenv("OCR_PROVIDER", "textin")
    result = run_ocr(sys.argv[2:], sys.argv[1], provider=provider)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
