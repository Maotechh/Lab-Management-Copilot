"""Print a lab prompt in an isolated Windows console for the integration test."""

import base64
import re

if __name__ == "__main__":
    while True:
        try:
            command = input("teacher@hxsy-zngl:~$ ")
        except EOFError:
            break
        token = re.search(r"LAB:%s:BEGIN\\n' ([0-9a-f]{10})", command)
        if not token or "printf fixture" not in command:
            raise SystemExit(2)
        print(f"LAB:{token[1]}:BEGIN", flush=True)
        print(base64.b64encode("\u4e2d\u6587\nfixture stderr\n".encode()).decode(), flush=True)
        print(f"\nLAB:{token[1]}:END:17", flush=True)
