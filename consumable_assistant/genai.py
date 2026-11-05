from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from .settings import (
    GENAI_API_KEY,
    GENAI_BASE_URL,
    GENAI_COMPLETION_URL,
    GENAI_MAX_OUTPUT_TOKENS,
    GENAI_MODE,
    GENAI_MODEL,
    GENAI_PROVIDER_TIMEOUT,
    GENAI_RESPONSE_URL,
    GENAI_TEMPERATURE,
    GENAI_TOTAL_TIMEOUT,
)

AI_REQUIRED = os.environ.get("GENAI_REQUIRED", "1") != "0"
HEALTH_LOCK = threading.Lock()
LAST_HEALTH_CHECK = 0.0
LOGGER = logging.getLogger(__name__)
DEFAULT_FALLBACK_PROVIDERS = (
    {
        "name": "Qwen3.8-Flash-Next-FP8",
        "model": "qwen-instruct",
        "base_url": "http://10.15.171.204:30277/v1",
    },
    {
        "name": "GLM-5.3-Flash",
        "model": "glm-chat",
        "base_url": "http://10.15.171.204:30155/v1",
    },
    {
        "name": "DeepSeek 校内直连接口",
        "model": "deepseek-pro",
        "base_url": "http://10.15.171.204:30969/v1",
    },
    {
        "name": "Kimi-K3",
        "model": "kimi",
        "base_url": "http://10.15.171.204:30515/v1",
        "timeout": 20,
    },
)
MAX_PROVIDER_CALLS = 8
HEALTH_INTERVAL = 300


class AIUnavailable(RuntimeError):
    pass


def record_health(state: str, detail: str) -> None:
    from .database import connect

    try:
        with connect() as conn:
            conn.execute(
                "INSERT INTO service_health(name,status,detail) VALUES ('ai',?,?) ON CONFLICT(name) DO UPDATE SET status=excluded.status,detail=excluded.detail,checked_at=CURRENT_TIMESTAMP",
                (state, detail),
            )
    except sqlite3.Error:
        LOGGER.warning("Unable to persist AI health status")


def status() -> dict[str, Any]:
    from .database import connect

    result = {
        "configured": model_is_enabled(),
        "required": AI_REQUIRED,
        "model": GENAI_MODEL,
        "status": "unknown",
        "checked_at": None,
        "provider": None,
        "providers": [],
    }
    try:
        with connect() as conn:
            row = conn.execute(
                "SELECT * FROM service_health WHERE name='ai'"
            ).fetchone()
        if row:
            result.update(dict(row))
            checked = datetime.fromisoformat(row["checked_at"])
            if checked.tzinfo is None:
                checked = checked.replace(tzinfo=UTC)
            if (datetime.now(UTC) - checked).total_seconds() > 600:
                result["status"] = "stale"
    except (sqlite3.Error, ValueError):
        LOGGER.warning("Unable to read AI health status")
    if not result["configured"]:
        result["status"] = "unconfigured"
    try:
        with connect() as conn:
            result["providers"] = [
                dict(row)
                for row in conn.execute(
                    "SELECT provider,model,status,checked_at,elapsed_ms,error_type FROM ai_provider_checks ORDER BY checked_at DESC"
                )
            ]
        available = [item for item in result["providers"] if item["status"] == "available"]
        if available:
            result["provider"] = available[0]["provider"]
    except sqlite3.Error:
        LOGGER.warning("Unable to read model provider checks")
    return result


def check_health() -> dict[str, Any]:
    global LAST_HEALTH_CHECK
    with HEALTH_LOCK:
        if time.monotonic() - LAST_HEALTH_CHECK < 60:
            return status()
        result = _check_health()
        LAST_HEALTH_CHECK = time.monotonic()
        return result


def _check_health() -> dict[str, Any]:
    if not model_is_enabled():
        record_health("unconfigured", "AI 凭据尚未配置")
    else:
        try:
            parsed = request_json(
                SYSTEM_PROMPT,
                build_prompt("100mL烧杯入库1个"),
                validator=lambda value: bool(
                    isinstance(value.get("operations"), list)
                    and any(
                        op.get("action") == "inbound"
                        and str(op.get("quantity")) in ("1", "1.0")
                        for op in value["operations"]
                        if isinstance(op, dict)
                    )
                ),
                max_output_tokens=96,
            )
            if not parsed:
                raise AIUnavailable("模型没有返回可用操作")
            record_health("available", str(parsed.get("_provider") or parsed.get("_model")))
        except (AIUnavailable, httpx.HTTPError, ValueError):
            record_health("unavailable", "校内模型请求未成功，正在等待下次检查")
    return status()


def model_is_enabled() -> bool:
    return bool(GENAI_API_KEY.strip() or provider_entries())


def parse_inventory_message(text: str) -> dict[str, Any] | None:
    if not model_is_enabled():
        return None
    parsed = request_json(
        SYSTEM_PROMPT,
        build_prompt(text),
        validator=valid_inventory_result,
        max_output_tokens=GENAI_MAX_OUTPUT_TOKENS,
    )
    if not parsed:
        record_health("unavailable", "校内模型请求在有限重试后未成功")
        raise AIUnavailable("校内 AI 暂时不可用，请稍后重试")
    model = str(parsed.pop("_model", GENAI_MODEL))
    provider = str(parsed.pop("_provider", ""))
    parsed["model"] = model
    parsed["provider"] = provider
    record_health("available", f"{provider} ({model})")
    return parsed


def valid_inventory_result(value: dict[str, Any]) -> bool:
    operations = value.get("operations")
    return bool(
        isinstance(operations, list)
        and operations
        and len(operations) <= 200
        and all(
            isinstance(op, dict)
            and op.get("action")
            in (
                "inbound",
                "consume",
                "borrow",
                "return",
                "adjust",
                "threshold",
                "needs_review",
            )
            for op in operations
        )
    )


def provider_entries() -> list[dict[str, Any]]:
    primary = {
        "name": os.environ.get("GENAI_PRIMARY_NAME", "上海科技大学 GenAI"),
        "model": GENAI_MODEL,
        "base_url": GENAI_BASE_URL,
        "mode": GENAI_MODE,
        "api_key": GENAI_API_KEY,
        "primary": True,
    }
    entries = [primary] if primary["base_url"].strip() else []
    for model in os.environ.get("GENAI_FALLBACK_MODELS", "").split(","):
        model = model.strip()
        if model and model != GENAI_MODEL and primary["base_url"].strip():
            entries.append({**primary, "name": model, "model": model, "primary": False})
    raw = os.environ.get("GENAI_FALLBACK_PROVIDERS_JSON", "").strip()
    try:
        fallbacks = json.loads(raw) if raw else list(DEFAULT_FALLBACK_PROVIDERS)
    except json.JSONDecodeError as exc:
        raise AIUnavailable("GENAI_FALLBACK_PROVIDERS_JSON 格式无效") from exc
    if not isinstance(fallbacks, list):
        raise AIUnavailable("GENAI_FALLBACK_PROVIDERS_JSON 必须是数组")
    for item in fallbacks:
        if not isinstance(item, dict):
            raise AIUnavailable("备用模型配置无效")
        name, model, base_url = (
            str(item.get(key) or "").strip()
            for key in ("name", "model", "base_url")
        )
        if not name or not model or not base_url.startswith(("http://", "https://")):
            raise AIUnavailable("备用模型配置需要名称、模型和 HTTP 地址")
        entries.append(
            {
                "name": name,
                "model": model,
                "base_url": base_url.rstrip("/"),
                "mode": "openai",
                "api_key": str(item.get("api_key") or ""),
                "primary": False,
            }
        )
    unique = []
    seen = set()
    for item in entries:
        marker = (item["base_url"], item["model"])
        if marker not in seen:
            unique.append(item)
            seen.add(marker)
    return unique[:MAX_PROVIDER_CALLS]


def request_json(
    system: str,
    prompt: str,
    validator=None,
    max_output_tokens: int | None = None,
    deadline_at: float | None = None,
) -> dict[str, Any] | None:
    providers = provider_entries()
    if not providers:
        return None
    deadline = time.monotonic() + GENAI_TOTAL_TIMEOUT
    if deadline_at is not None:
        deadline = min(deadline, deadline_at)
    max_tokens = max_output_tokens or GENAI_MAX_OUTPUT_TOKENS
    errors = []
    calls = 0
    attempted = []

    for pass_index in range(2):
        for provider in providers:
            if calls >= MAX_PROVIDER_CALLS:
                break
            if pass_index and provider not in attempted:
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0.25:
                break
            calls += 1
            if not pass_index:
                attempted.append(provider)
            started = time.monotonic()
            try:
                payload = provider_request(provider, system, prompt, max_tokens)
                provider_timeout = max(
                    0.25,
                    min(float(provider.get("timeout", GENAI_PROVIDER_TIMEOUT)), remaining),
                )
                with httpx.Client(
                    timeout=httpx.Timeout(
                        provider_timeout,
                        connect=min(3.0, provider_timeout),
                    )
                ) as client:
                    response = client.post(
                        payload["url"],
                        json=payload["body"],
                        headers=payload["headers"],
                    )
                    response.raise_for_status()
                    body = response.json()
                parsed = parse_model_json(extract_text(body) or "")
                if not parsed or validator and not validator(parsed):
                    raise AIUnavailable("模型返回内容未通过格式检查")
                parsed["_model"] = extract_model(body) or provider["model"]
                parsed["_provider"] = provider["name"]
                record_provider_check(provider, "available", started, None)
                return parsed
            except (httpx.HTTPError, ValueError, AIUnavailable) as exc:
                errors.append(type(exc).__name__)
                record_provider_check(provider, "unavailable", started, type(exc).__name__)
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (401, 403):
                    LOGGER.warning("Institutional model rejected credentials for %s", provider["name"])
            if time.monotonic() >= deadline:
                break
        if calls >= MAX_PROVIDER_CALLS or time.monotonic() >= deadline:
            break
    LOGGER.warning("All configured school model providers failed: %s", ",".join(errors[-8:]))
    return None


def provider_request(
    provider: dict[str, Any], system: str, prompt: str, max_tokens: int
) -> dict[str, Any]:
    mode = provider.get("mode", "openai")
    if mode == "response":
        body = {
            "model": provider["model"],
            "input": f"{system}\n\n{prompt}",
            "instructions": system,
            "temperature": GENAI_TEMPERATURE,
            "max_output_tokens": max_tokens,
            "stream": False,
        }
        url = provider["base_url"]
    elif mode == "completion":
        body = {
            "model": provider["model"],
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "temperature": GENAI_TEMPERATURE,
            "max_tokens": max_tokens,
            "stream": False,
        }
        url = provider["base_url"]
    else:
        url = provider["base_url"].rstrip("/") + "/chat/completions"
        body = {
            "model": provider["model"],
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "temperature": GENAI_TEMPERATURE,
            "max_tokens": max_tokens,
            "stream": False,
            "response_format": {"type": "json_object"},
        }
    headers = {"Content-Type": "application/json"}
    if provider.get("api_key"):
        headers["Authorization"] = f"Bearer {provider['api_key']}"
    return {"url": url, "body": body, "headers": headers}


def record_provider_check(
    provider: dict[str, Any], state: str, started: float, error_type: str | None
) -> None:
    from .database import connect

    elapsed = int((time.monotonic() - started) * 1000)
    checked_at = datetime.now(UTC).isoformat(timespec="microseconds")
    try:
        with connect() as conn:
            conn.execute(
                "INSERT INTO ai_provider_checks(provider,model,status,checked_at,elapsed_ms,error_type) VALUES (?,?,?,?,?,?) ON CONFLICT(provider) DO UPDATE SET model=excluded.model,status=excluded.status,checked_at=excluded.checked_at,elapsed_ms=excluded.elapsed_ms,error_type=excluded.error_type",
                (provider["name"], provider["model"], state, checked_at, elapsed, error_type),
            )
    except sqlite3.Error:
        LOGGER.warning("Unable to persist model provider status")


def build_request(text: str) -> tuple[str, dict[str, Any]]:
    if GENAI_MODE == "response":
        endpoint = GENAI_BASE_URL or GENAI_RESPONSE_URL
        payload = {
            "model": GENAI_MODEL,
            "input": build_prompt(text),
            "instructions": SYSTEM_PROMPT,
            "temperature": GENAI_TEMPERATURE,
            "max_output_tokens": GENAI_MAX_OUTPUT_TOKENS,
            "stream": False,
        }
        return endpoint, payload
    endpoint = GENAI_BASE_URL or GENAI_COMPLETION_URL
    payload = {
        "model": GENAI_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_prompt(text)},
        ],
        "temperature": GENAI_TEMPERATURE,
        "stream": False,
    }
    return endpoint, payload


def build_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {GENAI_API_KEY}",
        "Content-Type": "application/json",
    }


SYSTEM_PROMPT = """
你是一个严格的化学实验耗材库存解析器。
只返回一个合法 JSON 对象，不要解释，不要输出代码块。

返回字段：
{
  "operations": [
    {
      "action": "inbound|consume|borrow|return|adjust|threshold|needs_review",
      "item_query": "用于检索的精简关键词",
      "item_name": "识别到的耗材名称，无法确定时为 null",
      "quantity": 数字或 null,
      "unit": "单位或 null",
      "threshold": 数字或 null,
      "note": "补充说明或 null",
      "needs_review": true 或 false,
      "confidence": 0 到 1 之间的小数
    }
  ]
}

规则：
1. 明确表示已经到货或入库时，action 用 inbound。“准备采购、计划购买、预计到货”用 needs_review，不能增加库存。
2. 说“用了、消耗、出库、报废、碎了、破损、不放回、用光、用完、用尽、耗尽”时，action 用 consume。
3. 说“借出、借用、暂借”时，action 用 borrow。
4. 说“归还、还回、放回”时，action 用 return。
5. 说“预警、阈值、少于、低于”时，action 用 threshold。
6. 说“修正、改成、调整为、校正为”时，action 用 adjust。
7. 如果耗材名称不明确或数量不明确，needs_review 设为 true。
8. 不要编造内容。item_query 只保留适合检索的简短关键词。
9. 如果输入里有多条库存动作，请按原顺序分别输出多条 operations，不要合并成一条。
10. 如果用户说“用光了、用完了、用尽了、耗尽了”但没有给出数字，quantity 保持 null，由库存系统按当前库存自动填写。
11. 否定、条件、未来计划或更正指令用 needs_review。“不放回”属于 consume，不能解析为 return。保留名称中的容量与型号，不得将不同规格合并。
""".strip()


def build_prompt(text: str) -> str:
    return (
        "请解析下面这条库存操作输入，并按要求输出 JSON。"
        "如果输入里有多条库存动作，请分别输出多条 operations，顺序与原文一致。\n"
        f"{text}\n"
    )


def extract_text(payload: Any) -> str | None:
    if payload is None:
        return None
    if isinstance(payload, str):
        return payload.strip() or None
    if isinstance(payload, list):
        for item in payload:
            text = extract_text(item)
            if text:
                return text
        return None
    if isinstance(payload, dict):
        for key in ("output_text", "text", "content", "response", "message", "answer"):
            if key in payload:
                text = extract_text(payload[key])
                if text:
                    return text
        for key in ("output", "choices", "data", "result"):
            if key in payload:
                text = extract_text(payload[key])
                if text:
                    return text
        return None
    return str(payload).strip() or None


def parse_model_json(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    if not stripped:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, re.DOTALL)
    if fenced:
        stripped = fenced.group(1)
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        try:
            data = json.loads(stripped[start : end + 1])
        except json.JSONDecodeError:
            return None
    if not isinstance(data, dict):
        return None
    return data


def extract_model(payload: Any) -> str | None:
    if isinstance(payload, dict):
        if isinstance(payload.get("model"), str):
            return payload["model"]
        data = payload.get("data")
        if isinstance(data, dict) and isinstance(data.get("model"), str):
            return data["model"]
    return None
