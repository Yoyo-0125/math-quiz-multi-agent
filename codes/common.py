import json
import os
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from budget import TokenBudget, estimate_tokens


DEFAULT_MODEL_NAME = "deepseek-v4-flash"
DEFAULT_API_BASE = "https://api.deepseek.com"
MIN_THINKING_MAX_TOKENS = 8000
_TOKEN_BUDGET = None


class EmptyModelContentError(RuntimeError):
    pass


def set_token_budget(options):
    global _TOKEN_BUDGET

    if not options:
        _TOKEN_BUDGET = None
        return None

    _TOKEN_BUDGET = TokenBudget(
        total_warning=options.get("total_warning", 180000),
        total_stop=options.get("total_stop", 270000),
        single_warning=options.get("single_warning", 45000),
        single_stop=options.get("single_stop", 65000),
    )
    return _TOKEN_BUDGET


def get_token_usage_summary():
    if _TOKEN_BUDGET is None:
        return None
    return _TOKEN_BUDGET.summary()


def find_env_file(env_path=".env"):
    candidates = [
        Path(env_path),
        Path.cwd() / env_path,
        Path(__file__).resolve().parent.parent / env_path,
    ]

    for path in candidates:
        if path.exists():
            return path

    return None


def load_env_file(env_path=".env"):
    path = find_env_file(env_path)

    if path is None:
        return

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()

        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        elif value.startswith("'") and value.endswith("'"):
            value = value[1:-1]

        # Runtime options from config/pipeline_options.json take precedence.
        os.environ.setdefault(key, value)


def get_api_key():
    load_env_file()
    api_key = os.getenv("DEEPSEEK_API_KEY")

    if not api_key:
        raise RuntimeError(
            "没有找到 DEEPSEEK_API_KEY。请在项目根目录创建 .env 文件，"
            "可参考 .env.example。"
        )

    return api_key


def get_api_base():
    load_env_file()
    return os.getenv("DEEPSEEK_API_BASE", DEFAULT_API_BASE).rstrip("/")


def get_model_name(agent_name=None, default_model=DEFAULT_MODEL_NAME):
    load_env_file()

    if agent_name:
        env_key = f"DEEPSEEK_{agent_name.upper()}_MODEL"
        model_name = os.getenv(env_key)
        if model_name:
            return model_name

    return os.getenv("DEEPSEEK_MODEL", default_model)


def normalize_agent_name(agent_name):
    if not agent_name:
        return None
    return agent_name.replace("_revise", "")


def get_thinking_options(agent_name=None):
    load_env_file()
    normalized = normalize_agent_name(agent_name)

    if not normalized:
        return False, None

    thinking_value = os.getenv(f"DEEPSEEK_{normalized.upper()}_THINKING", "disabled")
    thinking_enabled = thinking_value.strip().lower() in [
        "1",
        "true",
        "yes",
        "enabled",
        "on",
    ]
    effort = os.getenv(f"DEEPSEEK_{normalized.upper()}_REASONING_EFFORT")

    return thinking_enabled, effort


def supports_json_response_format(model_name):
    return "reasoner" not in model_name.lower()


def write_debug_file(agent_name, filename_part, text, debug_dir="outputs/debug"):
    debug_path = Path(debug_dir)
    debug_path.mkdir(parents=True, exist_ok=True)

    safe_agent_name = "".join(
        ch if ch.isalnum() or ch in "_-" else "_"
        for ch in agent_name
    )

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    path = debug_path / f"{timestamp}_{safe_agent_name}_{filename_part}.txt"
    path.write_text(text, encoding="utf-8")
    return path


def call_deepseek_raw(
    system_prompt,
    user_content,
    max_tokens=2000,
    model_name=None,
    response_format_json=None,
    thinking_enabled=False,
    reasoning_effort=None,
    agent_name="unknown",
):
    api_key = get_api_key()
    selected_model = model_name or get_model_name()
    url = f"{get_api_base()}/chat/completions"

    if thinking_enabled and max_tokens < MIN_THINKING_MAX_TOKENS:
        max_tokens = MIN_THINKING_MAX_TOKENS

    if response_format_json is None:
        response_format_json = supports_json_response_format(selected_model) and not thinking_enabled

    payload = {
        "model": selected_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "max_tokens": max_tokens,
        "stream": False,
    }

    if response_format_json:
        payload["response_format"] = {"type": "json_object"}

    if selected_model.startswith("deepseek-v4"):
        payload["thinking"] = {"type": "enabled" if thinking_enabled else "disabled"}
    elif thinking_enabled:
        payload["thinking"] = {"type": "enabled"}

    if thinking_enabled and reasoning_effort:
        payload["reasoning_effort"] = reasoning_effort

    prompt_tokens = estimate_tokens(system_prompt) + estimate_tokens(user_content)
    estimated_total_tokens = prompt_tokens + int(max_tokens)
    if _TOKEN_BUDGET is not None:
        estimated_total_tokens = _TOKEN_BUDGET.check_request(
            agent_name=agent_name or selected_model,
            prompt_tokens=prompt_tokens,
            max_output_tokens=max_tokens,
        )

    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            response_text = response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        error_body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"请求 DeepSeek API 失败，HTTP {error.code}: {error_body}") from error
    except Exception as error:
        raise RuntimeError(f"请求 DeepSeek API 失败：{error}") from error

    try:
        response_json = json.loads(response_text)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"API 返回的不是合法 JSON：{response_text}") from error

    if "error" in response_json:
        raise RuntimeError(response_json["error"])

    if _TOKEN_BUDGET is not None:
        _TOKEN_BUDGET.record_usage(
            agent_name=agent_name or selected_model,
            usage=response_json.get("usage"),
            fallback_total=estimated_total_tokens,
        )

    message = response_json["choices"][0]["message"]
    content = message.get("content", "")

    if not content:
        reasoning_content = message.get("reasoning_content", "")
        raise EmptyModelContentError(
            "模型返回了空内容。thinking mode 可能消耗了输出预算，"
            f"reasoning_content 长度：{len(reasoning_content)}。"
        )

    return content


def repair_json_text(bad_json_text, error_message, agent_name, max_tokens=2000):
    repair_system_prompt = """
你是一个 JSON 修复器。
你的任务是把一段看起来像 JSON 但格式不合法的文本修复为合法 JSON。
要求：
1. 只输出修复后的合法 JSON。
2. 不要输出 Markdown。
3. 不要解释。
4. 不要改变原始含义。
5. 如果字符串内部有英文双引号，请改成中文书名号、中文引号，或正确转义。
6. 保留原有字段名和数据结构。
"""

    repair_user_content = f"""
下面是 agent {agent_name} 输出的非法 JSON。
JSON 解析错误：
{error_message}

非法 JSON 原文：
{bad_json_text}
"""

    return call_deepseek_raw(
        system_prompt=repair_system_prompt,
        user_content=repair_user_content,
        max_tokens=max_tokens,
        agent_name=f"{agent_name}_json_repair",
    )


def call_deepseek_json(
    system_prompt,
    user_content,
    max_tokens=2000,
    agent_name="unknown",
    max_retries=2,
    model_name=None,
    response_format_json=None,
):
    thinking_enabled, reasoning_effort = get_thinking_options(agent_name)

    try:
        content = call_deepseek_raw(
            system_prompt=system_prompt,
            user_content=user_content,
            max_tokens=max_tokens,
            model_name=model_name,
            response_format_json=response_format_json,
            thinking_enabled=thinking_enabled,
            reasoning_effort=reasoning_effort,
            agent_name=agent_name,
        )
    except EmptyModelContentError:
        print(f"{agent_name} 返回空内容，自动以 thinking=disabled 重试一次。")
        content = call_deepseek_raw(
            system_prompt=system_prompt,
            user_content=user_content,
            max_tokens=max_tokens,
            model_name=model_name,
            response_format_json=response_format_json,
            thinking_enabled=False,
            reasoning_effort=None,
            agent_name=f"{agent_name}_retry_no_thinking",
        )

    write_debug_file(agent_name, "raw_attempt0", content)

    for attempt in range(max_retries + 1):
        try:
            return json.loads(content)

        except json.JSONDecodeError as error:
            error_message = (
                f"{error.msg}, line {error.lineno}, "
                f"column {error.colno}, char {error.pos}"
            )

            debug_path = write_debug_file(
                agent_name,
                f"invalid_attempt{attempt}",
                f"错误：{error_message}\n\n原始输出：\n{content}",
            )

            print(f"{agent_name} 输出了非法 JSON，已保存到：{debug_path}")

            if attempt >= max_retries:
                print("模型原始输出：")
                print(content)
                raise RuntimeError("模型没有返回合法 JSON，自动修复也失败") from error

            content = repair_json_text(
                bad_json_text=content,
                error_message=error_message,
                agent_name=agent_name,
                max_tokens=max_tokens,
            )

            write_debug_file(agent_name, f"repaired_attempt{attempt + 1}", content)


def read_text_file(path):
    return Path(path).read_text(encoding="utf-8")


def read_json_file(path):
    text = Path(path).read_text(encoding="utf-8")
    return json.loads(text)


def write_json_file(path, data):
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def write_text_file(path, text):
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(text, encoding="utf-8")
