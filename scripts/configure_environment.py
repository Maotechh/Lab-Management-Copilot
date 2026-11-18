from __future__ import annotations

import argparse
import http.client
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from consumable_assistant.settings import SCHOOL_RESPONSE_URL


def values(lines: list[str]) -> dict[str, str]:
    return {
        line.split("=", 1)[0].strip(): line.split("=", 1)[1].strip()
        for line in lines
        if "=" in line and not line.lstrip().startswith("#")
    }


def preflight(path: Path) -> None:
    env = {key: value.strip("\"'") for key, value in values(path.read_text(encoding="utf-8").splitlines()).items()}
    if not env.get("GENAI_API_KEY", "").strip() or env.get("GENAI_BASE_URL") != SCHOOL_RESPONSE_URL:
        raise SystemExit("Set a replacement GENAI_API_KEY and the exact school HTTPS GENAI_BASE_URL in the protected application environment before deployment")
    if env.get("GENAI_MODE", "responses") != "responses" or env.get("GENAI_MODEL", "deepseek-pro") != "deepseek-pro":
        raise SystemExit("Set GENAI_MODE=responses and GENAI_MODEL=deepseek-pro before deployment")
    endpoint = urlsplit(SCHOOL_RESPONSE_URL)
    connection = http.client.HTTPSConnection(endpoint.hostname, timeout=45)
    try:
        connection.request("POST", endpoint.path, json.dumps({"model": "deepseek-pro", "input": "Reply OK.",
                           "instructions": "Answer briefly.", "stream": False, "temperature": 0.5, "max_output_tokens": 1000}),
                           {"Content-Type": "application/json", "Authorization": "Bearer " + env["GENAI_API_KEY"]})
        with connection.getresponse() as response:
            if response.status != 200:
                raise SystemExit(f"School preflight returned HTTP {response.status}; production writers have not been paused")
    except (http.client.HTTPException, OSError, ValueError):
        raise SystemExit("School HTTPS preflight failed; production writers have not been paused") from None
    finally:
        connection.close()


def configure(path: Path) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    existing = values(lines)
    updates = {
        "GENAI_REQUIRED": "1",
        "GENAI_MODEL": "deepseek-pro",
        "GENAI_MODE": "responses",
        "GENAI_PRIMARY_NAME": "学校 DeepSeek",
        "GENAI_BASE_URL": SCHOOL_RESPONSE_URL,
        "GENAI_API_KEY": "",
        "GENAI_MAX_OUTPUT_TOKENS": "1000",
        "GENAI_TEMPERATURE": "0.5",
        "GENAI_TOTAL_TIMEOUT": "300",
        "GENAI_PROVIDER_TIMEOUT": "300",
        "GENAI_FALLBACK_MODELS": "",
        "GENAI_FALLBACK_PROVIDERS_JSON": "[]",
        "LAB_DSH_URL": "http://127.0.0.1:8766",
        "PUBLIC_BASE_URL": "https://10.15.21.115",
        "VAPID_SUBJECT": "https://github.com/Maotechh/Lab-Management-Copilot/issues",
    }
    if existing.get("GENAI_API_KEY", "").strip("\"'"):
        endpoint = urlsplit(existing.get("GENAI_BASE_URL", "").strip("\"'"))
        if (endpoint.scheme != "https" or not endpoint.hostname or endpoint.username is not None
                or endpoint.password is not None or endpoint.query or endpoint.fragment):
            raise ValueError("A keyed model requires an explicit HTTPS GENAI_BASE_URL without credentials or query parameters")
        for key in ("GENAI_API_KEY", "GENAI_BASE_URL", "GENAI_MODE", "GENAI_MODEL", "GENAI_PRIMARY_NAME"):
            if existing.get(key, "").strip("\"'"):
                del updates[key]
    if not existing.get("VAPID_PRIVATE_KEY"):
        updates["VAPID_PRIVATE_KEY"] = "/etc/lab-management/push/vapid-private.pem"
    if not existing.get("VAPID_PUBLIC_KEY"):
        updates["VAPID_PUBLIC_KEY"] = (
            Path("/etc/lab-management/push/vapid-public.txt").read_text().strip()
        )
    result = [line for line in lines if line.split("=", 1)[0].strip() not in updates]
    result.extend(f"{key}={value}" for key, value in updates.items())
    path.write_text("\n".join(result) + "\n", encoding="utf-8")
    path.chmod(0o640)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    parser.add_argument("--preflight", action="store_true")
    args = parser.parse_args()
    (preflight if args.preflight else configure)(args.path)
