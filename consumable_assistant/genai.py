from __future__ import annotations

import json
import re
from typing import Any

import httpx

from .settings import (
    GENAI_API_KEY,
    GENAI_BASE_URL,
    GENAI_COMPLETION_URL,
    GENAI_MAX_OUTPUT_TOKENS,
    GENAI_MODE,
    GENAI_MODEL,
    GENAI_RESPONSE_URL,
    GENAI_TEMPERATURE,
    GENAI_TIMEOUT,
)


def model_is_enabled() -> bool:
    return bool(GENAI_API_KEY.strip())


def parse_inventory_message(text: str) -> dict[str, Any] | None:
    if not model_is_enabled():
        return None
    endpoint, payload = build_request(text)
    with httpx.Client(timeout=GENAI_TIMEOUT) as client:
        response = client.post(endpoint, json=payload, headers=build_headers())
        response.raise_for_status()
        data = response.json()
    raw_text = extract_text(data)
    if not raw_text:
        return None
    parsed = parse_model_json(raw_text)
    if parsed is not None:
        parsed.setdefault("model", extract_model(data) or GENAI_MODEL)
    return parsed


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
1. 说“入库、补充、买了、采购、增加”时，action 用 inbound。
2. 说“用了、消耗、出库、报废、碎了、破损、不放回”时，action 用 consume。
3. 说“借出、借用、暂借”时，action 用 borrow。
4. 说“归还、还回、放回”时，action 用 return。
5. 说“预警、阈值、少于、低于”时，action 用 threshold。
6. 说“修正、改成、调整为、校正为”时，action 用 adjust。
7. 如果耗材名称不明确或数量不明确，needs_review 设为 true。
8. 不要编造内容。item_query 只保留适合检索的简短关键词。
""".strip()


def build_prompt(text: str) -> str:
    return (
        "请解析下面这条库存操作输入，并按要求输出 JSON：\n"
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
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, re.S)
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
