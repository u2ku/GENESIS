"""SHIM — Anthropic Messages in front of an OpenAI-shaped endpoint.

Claude Code speaks three protocols: Anthropic Messages, Bedrock InvokeModel
and Vertex rawPredict. An OpenAI-shaped ``/v1/chat/completions`` endpoint is
not one of them. When GENESIS is pointed at a broker that only speaks
OpenAI — which is exactly what the local whooshd broker is — Claude Code
cannot talk to it directly, and the honest answer is not "it works".

This module is that answer. It is a translation layer: it speaks Anthropic
Messages on one side and OpenAI chat completions on the other, so a
OpenAI-only endpoint becomes usable by anything that speaks Anthropic.

**It is a shim, not a new model.** It does not improve the answer, reorder
the tools, or remember anything. It translates shapes and passes bytes.

Standard library only. It binds 127.0.0.1 and nothing else. The upstream
credential comes from the environment and is never written to a file, never
printed, and never put on a command line.

Run it directly:

    GENESIS_SHIM_UPSTREAM=http://127.0.0.1:8000/v1 \\
    GENESIS_SHIM_KEY=... \\
    python3 src/shim.py --port 8790
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# The upstream OpenAI base. The shim appends /chat/completions to it.
UPSTREAM_ENV = "GENESIS_SHIM_UPSTREAM"
KEY_ENV = "GENESIS_SHIM_KEY"
PORT_ENV = "GENESIS_SHIM_PORT"

DEFAULT_PORT = 8790

# Anthropic's stop reasons and OpenAI's finish reasons are not the same
# vocabulary. This is the whole of the mismatch on the non-streaming path.
FINISH_TO_STOP = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "end_turn",
}


# ------------------------------------------------------------------ request --

def _text_of(content) -> str:
    """The plain text inside an Anthropic content block list."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "".join(parts)


def to_openai(request: dict) -> dict:
    """Translate an Anthropic Messages request into an OpenAI one.

    The translation is deliberately literal. System prompt to a system
    message, content blocks to content, tool_use to tool_calls, tool_result
    to a tool message. Anything the two protocols cannot express is dropped
    rather than guessed at.
    """
    out: dict = {}
    model = request.get("model")
    if model:
        out["model"] = model
    if request.get("max_tokens") is not None:
        out["max_tokens"] = request["max_tokens"]
    if request.get("temperature") is not None:
        out["temperature"] = request["temperature"]
    if request.get("top_p") is not None:
        out["top_p"] = request["top_p"]
    if request.get("stop_sequences"):
        out["stop"] = request["stop_sequences"]
    if request.get("stream"):
        out["stream"] = True

    messages: list[dict] = []

    system = request.get("system")
    if system:
        text = _text_of(system)
        if text:
            messages.append({"role": "system", "content": text})

    for message in request.get("messages") or []:
        role = message.get("role")
        content = message.get("content")

        if isinstance(content, str):
            messages.append({"role": role, "content": content})
            continue

        if not isinstance(content, list):
            continue

        text_parts = []
        tool_calls = []
        tool_results = []
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text":
                text_parts.append(block.get("text", ""))
            elif kind == "thinking":
                # Extended thinking is not a concept OpenAI has. Dropping the
                # block is better than inventing a role for it.
                continue
            elif kind == "tool_use":
                tool_calls.append({
                    "id": block.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": json.dumps(block.get("input", {}),
                                                ensure_ascii=False),
                    },
                })
            elif kind == "tool_result":
                tool_results.append({
                    "role": "tool",
                    "tool_call_id": block.get("tool_use_id", ""),
                    "content": _text_of(block.get("content"))
                    if not isinstance(block.get("content"), str)
                    else block.get("content", ""),
                })

        if tool_results:
            # A tool result is its own OpenAI message, and it must follow
            # the assistant turn that called the tool.
            messages.extend(tool_results)
            if text_parts:
                messages.append({"role": role, "content": "".join(text_parts)})
            continue

        entry: dict = {"role": role}
        if tool_calls:
            entry["content"] = "".join(text_parts) or None
            entry["tool_calls"] = tool_calls
        else:
            entry["content"] = "".join(text_parts)
        messages.append(entry)

    out["messages"] = messages

    tools = request.get("tools")
    if tools:
        out["tools"] = [{
            "type": "function",
            "function": {
                "name": tool.get("name", ""),
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema") or {"type": "object"},
            },
        } for tool in tools if isinstance(tool, dict)]

    if request.get("tool_choice"):
        choice = request["tool_choice"]
        if isinstance(choice, dict) and choice.get("type") == "auto":
            out["tool_choice"] = "auto"
        elif isinstance(choice, dict) and choice.get("type") == "any":
            out["tool_choice"] = "required"
        elif isinstance(choice, dict) and choice.get("type") == "tool":
            out["tool_choice"] = {"type": "function",
                                  "function": {"name": choice.get("name", "")}}

    return out


# ----------------------------------------------------------------- response --

def _tool_input(arguments) -> dict:
    """The tool input, as a dict. OpenAI sends a JSON string."""
    if isinstance(arguments, dict):
        return arguments
    if not arguments:
        return {}
    try:
        parsed = json.loads(arguments)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def from_openai(response: dict, model: str | None = None) -> dict:
    """Translate an OpenAI chat completion into an Anthropic Message."""
    choices = response.get("choices") or []
    choice = choices[0] if choices else {}
    message = choice.get("message") or {}

    content: list[dict] = []
    text = message.get("content")
    if text:
        content.append({"type": "text", "text": text})

    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        content.append({
            "type": "tool_use",
            "id": call.get("id", ""),
            "name": function.get("name", ""),
            "input": _tool_input(function.get("arguments")),
        })

    usage = response.get("usage") or {}
    return {
        "id": "msg_" + str(response.get("id", "")).removeprefix("chatcmpl-"),
        "type": "message",
        "role": "assistant",
        "model": model or response.get("model", ""),
        "content": content,
        "stop_reason": FINISH_TO_STOP.get(choice.get("finish_reason") or "",
                                          "end_turn"),
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


# ---------------------------------------------------------------- streaming --

class StreamTranslator:
    """Turn an OpenAI SSE stream into Anthropic's event sequence.

    Anthropic's streaming protocol is a sequence of named events with a
    fixed shape; OpenAI's is a sequence of untyped chunks. The mapping is
    mechanical but not trivial: content arrives as deltas that have to be
    wrapped in a start/stop pair, tool calls arrive as indexed fragments that
    have to be gathered into whole blocks, and the whole thing has to end with
    a message_delta and a message_stop.

    The translator is stateful and holds the open blocks. It is fed one
    OpenAI chunk at a time and yields the Anthropic events that chunk
    produced.
    """

    def __init__(self, model: str = ""):
        self.model = model
        self.started = False
        self.text_open = False
        self.tool_blocks: dict[int, dict] = {}
        self.next_index = 0
        self.finish_reason = None
        self.input_tokens = 0
        self.output_tokens = 0

    def _event(self, name: str, payload: dict) -> str:
        return (f"event: {name}\n"
                f"data: {json.dumps(payload, ensure_ascii=False)}\n\n")

    def _start(self, model: str = "") -> list[str]:
        self.started = True
        message_id = "msg_shim"
        return [self._event("message_start", {
            "type": "message_start",
            "message": {
                "id": message_id,
                "type": "message",
                "role": "assistant",
                "model": model or self.model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        })]

    def _open_text(self) -> list[str]:
        if self.text_open:
            return []
        index = self.next_index
        self.next_index += 1
        self.text_open = True
        return [self._event("content_block_start", {
            "type": "content_block_start",
            "index": index,
            "content_block": {"type": "text", "text": ""},
        })]

    def _close_text(self) -> list[str]:
        if not self.text_open:
            return []
        index = self.next_index - 1
        self.text_open = False
        return [self._event("content_block_stop", {
            "type": "content_block_stop",
            "index": index,
        })]

    def feed(self, chunk: dict) -> list[str]:
        """One OpenAI chunk in; the Anthropic events it produces out."""
        events: list[str] = []
        if not self.started:
            events += self._start(chunk.get("model", ""))

        usage = chunk.get("usage") or {}
        if usage.get("prompt_tokens") is not None:
            self.input_tokens = usage["prompt_tokens"]
        if usage.get("completion_tokens") is not None:
            self.output_tokens = usage["completion_tokens"]

        choices = chunk.get("choices") or []
        choice = choices[0] if choices else {}
        if choice.get("finish_reason"):
            self.finish_reason = choice["finish_reason"]

        delta = choice.get("delta") or {}

        text = delta.get("content")
        if text:
            events += self._open_text()
            events.append(self._event("content_block_delta", {
                "type": "content_block_delta",
                "index": self.next_index - 1,
                "delta": {"type": "text_delta", "text": text},
            }))

        for call in delta.get("tool_calls") or []:
            position = call.get("index", 0)
            block = self.tool_blocks.get(position)
            if block is None:
                block = {"index": self.next_index, "id": call.get("id", ""),
                         "name": "", "open": False}
                self.next_index += 1
                self.tool_blocks[position] = block
                events += self._close_text()
            function = call.get("function") or {}
            if function.get("name") and not block["name"]:
                block["name"] = function["name"]
                block["open"] = True
                events.append(self._event("content_block_start", {
                    "type": "content_block_start",
                    "index": block["index"],
                    "content_block": {
                        "type": "tool_use",
                        "id": block["id"] or "tool_shim",
                        "name": block["name"],
                        "input": {},
                    },
                }))
            arguments = function.get("arguments")
            if arguments and block["open"]:
                events.append(self._event("content_block_delta", {
                    "type": "content_block_delta",
                    "index": block["index"],
                    "delta": {"type": "input_json_delta",
                              "partial_json": arguments},
                }))

        return events

    def close(self) -> list[str]:
        """The events that end the stream."""
        events: list[str] = []
        events += self._close_text()
        for block in self.tool_blocks.values():
            if block["open"]:
                events.append(self._event("content_block_stop", {
                    "type": "content_block_stop",
                    "index": block["index"],
                }))
        stop_reason = FINISH_TO_STOP.get(self.finish_reason or "", "end_turn")
        events.append(self._event("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": self.output_tokens},
        }))
        events.append(self._event("message_stop", {"type": "message_stop"}))
        return events


# ------------------------------------------------------------------- server --

class ShimError(Exception):
    """Something the upstream did that the shim cannot translate."""


def _post_json(url: str, payload: dict, key: str, timeout: float = 120.0):
    """POST to the upstream, with the credential in the header."""
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", "application/json")
    if key:
        request.add_header("Authorization", f"Bearer {key}")
    try:
        return urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise ShimError(f"upstream {exc.code}: {detail[:400]}") from exc
    except urllib.error.URLError as exc:
        raise ShimError(f"upstream unreachable: {exc.reason}") from exc


class Handler(BaseHTTPRequestHandler):
    """The Anthropic side of the shim."""

    server_version = "genesis-shim/1.0"
    protocol_version = "HTTP/1.1"

    # -- plumbing ----------------------------------------------------------
    def log_message(self, fmt, *args):  # noqa: A003 - base class name
        """Log to stderr, and never the credential.

        The request line carries no credential — the key travels in a header
        — but the rule stands regardless: the shim does not print secrets.
        """
        sys.stderr.write("shim: " + (fmt % args) + "\n")

    def _send(self, status: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            raise ShimError(f"the request body is not JSON: {exc}") from exc

    # -- routes ------------------------------------------------------------
    def do_POST(self):  # noqa: N802 - base class name
        upstream = self.server.shim_upstream  # type: ignore[attr-defined]
        key = self.server.shim_key  # type: ignore[attr-defined]
        path = self.path.split("?")[0].rstrip("/")

        try:
            request = self._read_json()
        except ShimError as exc:
            self._send(400, {"type": "error",
                             "error": {"type": "invalid_request_error",
                                       "message": str(exc)}})
            return

        if path.endswith("/count_tokens"):
            self._send(200, {"input_tokens": _estimate_tokens(request)})
            return

        if not path.endswith("/messages"):
            self._send(404, {"type": "error",
                             "error": {"type": "not_found_error",
                                       "message": f"no route for {path}"}})
            return

        payload = to_openai(request)
        target = upstream.rstrip("/") + "/chat/completions"

        if request.get("stream"):
            self._stream(target, payload, key, request.get("model", ""))
            return

        try:
            with _post_json(target, payload, key) as response:
                raw = response.read().decode("utf-8", "replace")
        except ShimError as exc:
            self._send(502, {"type": "error",
                             "error": {"type": "api_error", "message": str(exc)}})
            return

        try:
            parsed = json.loads(raw)
        except ValueError:
            self._send(502, {"type": "error",
                             "error": {"type": "api_error",
                                       "message": "the upstream did not return JSON"}})
            return

        self._send(200, from_openai(parsed, request.get("model")))

    def _stream(self, target: str, payload: dict, key: str, model: str):
        """Relay the upstream's SSE stream, translated."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        translator = StreamTranslator(model)
        try:
            with _post_json(target, payload, key) as response:
                for raw_line in response:
                    line = raw_line.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except ValueError:
                        continue
                    for event in translator.feed(chunk):
                        self.wfile.write(event.encode("utf-8"))
                        self.wfile.flush()
            for event in translator.close():
                self.wfile.write(event.encode("utf-8"))
                self.wfile.flush()
        except ShimError as exc:
            self.log_message("stream failed: %s", exc)
        except (BrokenPipeError, ConnectionResetError):
            pass


def _estimate_tokens(request: dict) -> int:
    """A rough token count. Count_tokens is advisory, not billing."""
    text = json.dumps(request.get("messages") or [], ensure_ascii=False)
    text += str(request.get("system") or "")
    return max(1, len(text) // 4)


def serve(upstream: str, key: str, port: int = DEFAULT_PORT):
    """Run the shim until it is stopped."""
    if not upstream:
        raise ShimError(f"no upstream: set {UPSTREAM_ENV}")
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.shim_upstream = upstream  # type: ignore[attr-defined]
    server.shim_key = key  # type: ignore[attr-defined]
    sys.stderr.write(f"shim: anthropic messages -> {upstream} on "
                     f"http://127.0.0.1:{port}\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="genesis-shim",
        description="Anthropic Messages in front of an OpenAI-shaped endpoint.")
    parser.add_argument("--upstream", default=os.environ.get(UPSTREAM_ENV, ""),
                        help="the OpenAI base URL (env: %s)" % UPSTREAM_ENV)
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get(PORT_ENV, DEFAULT_PORT)),
                        help="the port to listen on, 127.0.0.1 only")
    args = parser.parse_args(argv)

    key = os.environ.get(KEY_ENV, "")
    try:
        serve(args.upstream, key, args.port)
    except ShimError as exc:
        sys.stderr.write(f"shim: {exc}\n")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
