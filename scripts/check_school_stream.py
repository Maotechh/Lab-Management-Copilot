"""Probe school Responses streaming with text or a synthetic read-only tool."""

import argparse
import getpass
import http.client
import json
import secrets
import sys
import time


def events(response):
    data = []
    for line in response:
        line = line.rstrip(b"\r\n")
        if not line and data:
            yield b"\n".join(data)
            data.clear()
        elif line.startswith(b"data:"):
            data.append(line[5:].lstrip(b" "))


def stream_turn(key, inputs, instructions, tools=None):
    payload = {"model": "deepseek-pro", "instructions": instructions, "input": inputs,
               "max_output_tokens": 1000, "temperature": 0.5, "stream": True}
    if tools:
        payload["tools"] = tools
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    if key:
        headers["Authorization"] = "Bearer " + key
    connection = http.client.HTTPSConnection("genaiapi.shanghaitech.edu.cn", timeout=60)
    started = time.monotonic()
    try:
        connection.request("POST", "/api/v1/response", json.dumps(payload), headers)
        with connection.getresponse() as response:
            media = response.headers.get_content_type()
            print(f"HTTP {response.status}; {media}", flush=True)
            if response.status != 200 or media != "text/event-stream":
                return None
            text_parts = []
            output = []
            first = None
            for raw in events(response):
                if raw == b"[DONE]":
                    break
                event = json.loads(raw)
                kind = event.get("type")
                if kind == "response.output_text.delta" and isinstance(event.get("delta"), str) and event["delta"]:
                    text_parts.append(event["delta"])
                    if first is None:
                        first = time.monotonic() - started
                        print(f"first_text_delta_s={first:.3f}", flush=True)
                if kind == "response.output_item.done" and isinstance(event.get("item"), dict):
                    output.append(event["item"])
                if kind in {"error", "response.failed", "response.incomplete", "response.completed"}:
                    print(f"text_deltas={len(text_parts)}; terminal={kind}; total_s={time.monotonic()-started:.3f}")
                    return ("".join(text_parts), output) if kind == "response.completed" else None
            print(f"text_deltas={len(text_parts)}; no response.completed event")
    finally:
        connection.close()
    return None


def probe(key, tool=False):
    if not tool:
        result = stream_turn(key, "Count from one to ten in words.", "Answer in English.")
        return bool(result and result[0])

    name = "lookup_fixture_stock"
    sku = "TEST-BEAKER"
    tools = [{"type": "function", "name": name, "description": "Read the available quantity for a synthetic SKU.",
              "parameters": {"type": "object", "properties": {"sku": {"type": "string"}}, "required": ["sku"],
                             "additionalProperties": False}}]
    instructions = "Call the lookup tool exactly once for the requested SKU. After its result, return only the integer quantity."
    inputs = [{"role": "user", "content": f"How many units of {sku} are available?"}]
    first = stream_turn(key, inputs, instructions, tools)
    calls = [item for item in first[1] if item.get("type") == "function_call"] if first else []
    if len(calls) != 1 or calls[0].get("name") != name or not calls[0].get("call_id"):
        print("tool_call=missing_or_unexpected")
        return False
    call = calls[0]
    if not isinstance(call.get("arguments"), str) or json.loads(call["arguments"]) != {"sku": sku}:
        print("tool_arguments=invalid")
        return False
    print("tool_call=valid", flush=True)
    quantity = 10000 + secrets.randbelow(90000)
    inputs = [*inputs, *first[1], {"type": "function_call_output", "call_id": call["call_id"],
                                  "output": json.dumps({"available": quantity})}]
    second = stream_turn(key, inputs, instructions, tools)
    passed = bool(second and second[0].strip() == str(quantity)
                  and not any(item.get("type") == "function_call" for item in second[1]))
    print("tool_roundtrip=" + ("passed" if passed else "not_verified"))
    return passed


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-auth", action="store_true", help="check reachability only")
    parser.add_argument("--tool", action="store_true", help="check one synthetic tool call and its streamed follow-up")
    args = parser.parse_args()
    if not args.no_auth and not sys.stdin.isatty():
        parser.error("Run in an interactive terminal to enter a rotated key without echo")
    key = "" if args.no_auth else getpass.getpass("Rotated school gateway key (hidden): ").strip()
    if not args.no_auth and not key:
        parser.error("No key supplied")
    try:
        sys.exit(0 if probe(key, tool=args.tool) else 1)
    except (OSError, http.client.HTTPException, ValueError) as error:
        sys.exit(f"Probe did not complete: {type(error).__name__}")
