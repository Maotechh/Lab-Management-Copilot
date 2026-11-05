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
    if not existing.get("GENAI_API_KEY", "").strip("\"' "):
        raise RuntimeError(
            "Institutional AI credential must be configured before deployment"
        )
    updates = {"GENAI_REQUIRED": "1"}
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
