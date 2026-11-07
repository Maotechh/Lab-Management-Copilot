from __future__ import annotations

import sys
from pathlib import Path


def configure(path: Path) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    existing = {
        line.split("=", 1)[0].strip(): line.split("=", 1)[1].strip()
        for line in lines
        if "=" in line and not line.lstrip().startswith("#")
    }
    updates = {
        "GENAI_REQUIRED": "1",
        "GENAI_MODEL": "deepseek-pro",
        "GENAI_MODE": "responses",
        "GENAI_PRIMARY_NAME": "学校 DeepSeek",
        "GENAI_BASE_URL": "http://10.15.171.204:30969/v1",
        "GENAI_API_KEY": "",
        "GENAI_MAX_OUTPUT_TOKENS": "65536",
        "GENAI_TOTAL_TIMEOUT": "300",
        "GENAI_PROVIDER_TIMEOUT": "300",
        "GENAI_FALLBACK_MODELS": "",
        "GENAI_FALLBACK_PROVIDERS_JSON": "[]",
        "LAB_DSH_URL": "http://127.0.0.1:8766",
        "PUBLIC_BASE_URL": "https://10.15.21.115",
        "VAPID_SUBJECT": "https://github.com/Maotechh/Lab-Management-Copilot/issues",
    }
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
    configure(Path(sys.argv[1]))
