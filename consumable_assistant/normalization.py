from __future__ import annotations

import math
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Any


NULL_LIKE = {"", "nan", "none", "null", "na", "n/a"}

UNIT_WORDS = [
    "个",
    "支",
    "包",
    "盒",
    "台",
    "只",
    "套",
    "片",
    "根",
    "瓶",
    "筒",
    "袋",
    "卷",
    "块",
    "条",
    "把",
    "副",
    "份",
    "张",
    "箱",
    "批",
]

SPEC_UNITS = (
    "ml",
    "毫升",
    "l",
    "mm",
    "cm",
    "m",
    "℃",
    "°c",
    "mol",
    "g",
    "kg",
)

PUNCT_RE = re.compile(r"[\s,，.。;；:：、/\\|_\-+()（）\[\]【】{}<>《》\"'“”‘’]+")


def clean_cell(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    text = str(value).strip()
    if text.lower() in NULL_LIKE:
        return None
    return text


def normalize_text(value: Any) -> str:
    text = clean_cell(value) or ""
    text = unicodedata.normalize("NFKC", text).lower()
    text = text.replace("毫升", "ml")
    text = text.replace("公分", "cm")
    text = text.replace("厘米", "cm")
    text = text.replace("毫米", "mm")
    text = text.replace("升", "l")
    text = PUNCT_RE.sub("", text)
    return text


def parse_float(value: Any) -> float | None:
    text = clean_cell(value)
    if text is None:
        return None
    text = unicodedata.normalize("NFKC", text)
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(Decimal(match.group(0)))
    except (InvalidOperation, ValueError):
        return None


def normalize_quantity(value: float) -> float:
    if abs(value - round(value)) < 1e-9:
        return float(round(value))
    return value


def quantity_to_text(value: float | None) -> str:
    if value is None:
        return ""
    value = normalize_quantity(float(value))
    if value.is_integer():
        return str(int(value))
    return f"{value:g}"


def extract_aliases(name: str | None) -> list[str]:
    if not name:
        return []
    aliases: list[str] = []
    for match in re.finditer(r"[（(]([^（）()]+)[）)]", name):
        aliases.extend(split_alias_text(match.group(1)))
    aliases.extend(split_alias_text(name))
    unique: list[str] = []
    seen: set[str] = set()
    for alias in aliases:
        alias = alias.strip()
        key = normalize_text(alias)
        if not alias or not key or key == normalize_text(name) or key in seen:
            continue
        seen.add(key)
        unique.append(alias)
    return unique


def split_alias_text(text: str) -> list[str]:
    chunks = re.split(r"[/／、,，;；]", text)
    return [chunk.strip() for chunk in chunks if chunk and chunk.strip()]


CN_DIGITS = {
    "零": 0,
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


def chinese_number_to_int(text: str) -> int | None:
    if not text:
        return None
    total = 0
    current = 0
    for char in text:
        if char in CN_DIGITS:
            current = CN_DIGITS[char]
        elif char == "十":
            total += (current or 1) * 10
            current = 0
        elif char == "百":
            total += (current or 1) * 100
            current = 0
        else:
            return None
    return total + current


def stable_key(parts: list[Any]) -> str:
    return "|".join(normalize_text(part) for part in parts)
