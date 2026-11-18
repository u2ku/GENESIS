#!/usr/bin/env python3
"""LUNA PROBE — bisect what breaks the supergemma tune.

The `luna` backend's recorded model (supergemma4-26b-uncensored, a
community MLX tune) answers a plain hello cleanly and falls into a
`<|channel>thought` loop under the full chat payload. The loop is not
random: something in the payload trips the tune's chat template, and
this probe finds which ingredient it is, one variable at a time:

    the debug system prompt (prompt.debug.md, ~8 KB, tools section cut)
    the card alone (~4 KB)
    the real tool list (the JSON blocks under the tools marker)
    tools + system in combination

Each row prints `ok` or `SPAM`, so the trigger reads straight off the
output. The endpoint and model come from the codexify install's recorded
runtime — the same decision the chat itself rides on.

    python3 tools/luna_probe.py
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import harness  # noqa: E402


def _ask(endpoint: str, model: str, messages: list, tools: list | None):
    payload = {"model": model, "max_tokens": 1200, "messages": messages}
    if tools:
        payload["tools"] = tools
    req = urllib.request.Request(
        endpoint.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    body = json.loads(urllib.request.urlopen(req, timeout=120).read())
    return body["choices"][0]["message"]["content"] or ""


def _row(text: str, label: str) -> None:
    bad = "<|channel>" in text
    shown = text[:120].replace("\n", "\\n")
    print(f"{'SPAM' if bad else 'ok  '}  {label}"
          + ("" if bad else f"  -> {shown}"))


def main() -> int:
    base, model = harness.luna_runtime()
    endpoint = base if base.endswith("/v1") else base + "/v1"
    print(f"probing {model} at {endpoint}\n")

    raw = (ROOT / "prompt.debug.md").read_text(encoding="utf-8") \
        if (ROOT / "prompt.debug.md").exists() else ""
    if "<!-- genesis:tools -->" not in raw:
        print("prompt.debug.md is missing its tools marker — nothing to "
              "bisect. Run `genesis prompt --write` first.")
        return 2
    system, section = raw.split("<!-- genesis:tools -->", 1)
    system = system.rstrip()
    tools = []
    for block in re.findall(r"```json\n(.*?)```", section, re.S):
        spec = json.loads(block)
        tools.append({"type": "function", "function": {
            "name": spec["name"], "description": spec["description"],
            "parameters": spec["input_schema"]}})

    card = (ROOT / "card.md").read_text(encoding="utf-8")

    hello = {"role": "user", "content": "Hello"}
    cases = [
        ("bare hello, nothing else", [hello], None),
        ("debug system, no tools",
         [{"role": "system", "content": system}, hello], None),
        ("card, no tools",
         [{"role": "system", "content": card}, hello], None),
        ("debug system, real tools",
         [{"role": "system", "content": system}, hello], tools),
        ("card, real tools",
         [{"role": "system", "content": card}, hello], tools),
        ("no system, real tools", [hello], tools),
        ("debug system, 1 tool",
         [{"role": "system", "content": system}, hello], tools[:1]),
    ]
    for label, messages, tl in cases:
        try:
            _row(_ask(endpoint, model, messages, tl), label)
        except Exception as exc:
            print(f"ERR   {label}  -> {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
