"""Conservative identity matching across name/specification columns."""
import re
import unicodedata
from decimal import Decimal

from .normalization import normalize_text

_MEASURE = re.compile(r"(\d+(?:\.\d+)?)\s*(毫升|毫克|毫米|厘米|千克|升|克|ml|mm|cm|kg|mg|l|g)(?![a-z])", re.I)
_FACTORS = {"毫升": ("volume", "1"), "ml": ("volume", "1"), "升": ("volume", "1000"), "l": ("volume", "1000"),
            "毫米": ("length", "1"), "mm": ("length", "1"), "厘米": ("length", "10"), "cm": ("length", "10"),
            "毫克": ("mass", ".001"), "mg": ("mass", ".001"), "克": ("mass", "1"), "g": ("mass", "1"), "千克": ("mass", "1000"), "kg": ("mass", "1000")}


def identity(name, spec=""):
    value = unicodedata.normalize("NFKC", f"{name or ''} {spec or ''}").lower()
    measures = []
    def extract(match):
        kind, factor = _FACTORS[match[2].lower()]
        measures.append((kind, Decimal(match[1]) * Decimal(factor)))
        return ""
    residual = normalize_text(_MEASURE.sub(extract, value))
    return residual, tuple(sorted(set(measures)))


def same_material(name, spec, item_name, item_spec=""):
    return identity(name, spec) == identity(item_name, item_spec)


def physical_room(item):
    lab = str(item.get("lab") or "")
    rooms = {room.strip() for room in re.split(r"[/、,，+＋]", lab) if room.strip()}
    location = str(item.get("location_code") or "")
    match = re.match(r"^(\d{2,3})(?:\D|$)", location)
    if match:
        prefix = match[1]
        if prefix in rooms:
            return prefix
        # The source labels these materials as shared by rooms 307 and 310,
        # while their cabinet codes use the corresponding 07 and 10 prefixes.
        source_room = {"07": "307", "10": "310"}.get(prefix)
        if source_room in rooms:
            return source_room
    return lab
