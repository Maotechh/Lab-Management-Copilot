from __future__ import annotations

import base64
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


def generate(directory: Path) -> None:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    private = directory / "vapid-private.pem"
    if private.exists():
        raise SystemExit("Existing key must be preserved")
    key = ec.generate_private_key(ec.SECP256R1())
    private.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    private.chmod(0o600)
    public = (
        base64.urlsafe_b64encode(
            key.public_key().public_bytes(
                serialization.Encoding.X962,
                serialization.PublicFormat.UncompressedPoint,
            )
        )
        .decode()
        .rstrip("=")
    )
    (directory / "vapid-public.txt").write_text(public)
    print("VAPID_PRIVATE_KEY=" + str(private))
    print("VAPID_PUBLIC_KEY=" + public)


if __name__ == "__main__":
    generate(Path(os.environ.get("VAPID_KEY_DIRECTORY", "/etc/lab-management/push")))
