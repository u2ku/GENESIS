"""KEYDETECT — which gateway does this key belong to?

A key arrives with no name attached. The operator pastes a string and the
wizard has to decide three things from it: which provider issued it, which
endpoint serves it, and which variable name it belongs under. Getting the
third one wrong is not a cosmetic error — it is the difference between a
machine that speaks and a machine that 401s.

**A prefix is a guess about where to look, never a statement that the key
works.** `sk-ant-` says "Anthropic issued something like this"; it does not
say the key is valid, and it does not say the key is Anthropic's. The only
thing that settles it is the gateway answering. So this module does two
things and keeps them apart:

1. `sniff()` reads the format and produces an ordered list of **candidate
   endpoints** to probe. This is cheap, local, and never touches a network.
2. `probe()` asks a candidate endpoint, carrying the real credential, and
   asserts on the **body** — an Anthropic `"type": "message"` or an OpenAI
   `"choices"` array — not on the status code alone.

`docs/WIZARD.md` §5.5 is the rule this file implements: *the wizard does not
resolve a key by reading the prefix; it resolves it by asking the gateway.*
The prefix narrows the question from "every provider on earth" to "two or
three places worth asking". The gateway gives the answer.

**Measured, not assumed.** The prefix table below was checked against live
endpoints, and the results are recorded because they are the reason the table
looks the way it does:

- An `sk-or-` key authenticates at `https://openrouter.ai/api/v1/messages`
  with either `x-api-key` or `Authorization: Bearer` (both measured 200).
- An `sk-cp-` key authenticates at `https://api.minimax.io/anthropic/v1/messages`
  with `Authorization: Bearer` (measured 200), and OpenRouter **rejects the
  same key** with 401 and the text *"this is not an OpenRouter API key.
  OpenRouter keys start with sk-or-"*.
- So `sk-cp-` is the MiniMax family, and the OpenRouter/MiniMax bucket is a
  *family* rather than a single provider: the probe walks it and the gateway
  picks. A key filed under the wrong variable name — which is exactly what
  the GENESIS repo's own `.env` had — is caught by the walk rather than by
  an argument about prefixes.

Nothing here writes a credential, prints one, or puts one on a command line.
The key arrives as a string in memory and leaves as a fingerprint.

Standard library only.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

__all__ = [
    "ANTHROPIC", "OPENAI", "OPENROUTER", "MINIMAX", "PROVIDERS",
    "PROVIDER_CHOICES", "Endpoint", "Guess", "ProbeResult",
    "sniff", "detect", "probe", "probe_candidates", "fingerprint", "mask",
]


# --------------------------------------------------------------- the shapes --

# The two wire shapes a gateway can speak. Claude Code speaks the first; the
# shim exists to translate the second into the first.
ANTHROPIC = "anthropic"
OPENAI = "openai"


@dataclass(frozen=True)
class Endpoint:
    """One place a key might belong, and how to ask it.

    `base` is everything before the route, so the two shapes of the same
    provider carry different bases: OpenRouter's Anthropic route is
    `/api/v1/messages` and its OpenAI route is `/api/v1/chat/completions`,
    and Claude Code's `ANTHROPIC_BASE_URL` is the part in front of
    `/v1/messages`.
    """
    provider: str
    shape: str
    base: str
    env_var: str
    models: tuple
    auth: tuple = ("bearer",)
    note: str = ""

    @property
    def url(self) -> str:
        """The full route this endpoint answers on."""
        if self.shape == ANTHROPIC:
            return self.base.rstrip("/") + "/v1/messages"
        return self.base.rstrip("/") + "/chat/completions"

    @property
    def claude_base(self) -> Optional[str]:
        """The `ANTHROPIC_BASE_URL` Claude Code wants — or None."""
        return self.base if self.shape == ANTHROPIC else None

    def headers(self, key: str, style: str) -> dict:
        """The request headers for one auth style."""
        out = {"content-type": "application/json"}
        if style == "x-api-key":
            out["x-api-key"] = key
            out["anthropic-version"] = "2023-06-01"
        elif style == "bearer":
            out["authorization"] = "Bearer " + key
            if self.shape == ANTHROPIC:
                out["anthropic-version"] = "2023-06-01"
        return out


# ------------------------------------------------------------ the providers --

# The env var each provider's key belongs under. This is the part a human
# gets wrong — the same key under the wrong name authenticates nowhere, and
# the failure looks like a bad key rather than a bad filename.
PROVIDERS = {
    "anthropic": {
        "where": "Anthropic",
        "endpoints": (
            Endpoint("anthropic", ANTHROPIC, "https://api.anthropic.com",
                     "ANTHROPIC_API_KEY",
                     ("claude-haiku-4-5-20251001", "claude-sonnet-4-5-20250929"),
                     ("x-api-key",)),
        ),
    },
    "openai": {
        "where": "OpenAI",
        "endpoints": (
            Endpoint("openai", OPENAI, "https://api.openai.com/v1",
                     "OPENAI_API_KEY",
                     ("gpt-4.1-mini", "gpt-4o-mini"),
                     ("bearer",)),
        ),
    },
    "openrouter": {
        "where": "OpenRouter",
        "endpoints": (
            Endpoint("openrouter", ANTHROPIC, "https://openrouter.ai/api",
                     "OPENROUTER_API_KEY",
                     ("z-ai/glm-5.3", "anthropic/claude-sonnet-4.6"),
                     ("x-api-key", "bearer"),
                     "measured: accepts x-api-key or Bearer"),
            Endpoint("openrouter", OPENAI, "https://openrouter.ai/api/v1",
                     "OPENROUTER_API_KEY",
                     ("z-ai/glm-5.3",),
                     ("bearer",)),
        ),
    },
    "minimax": {
        "where": "MiniMax",
        "endpoints": (
            Endpoint("minimax", ANTHROPIC, "https://api.minimax.io/anthropic",
                     "MINIMAX_API_KEY",
                     ("MiniMax-M3", "MiniMax-M3.1-Flash"),
                     ("bearer", "x-api-key"),
                     "measured: Bearer answers 200"),
            Endpoint("minimax", OPENAI, "https://api.minimax.io/v1",
                     "MINIMAX_API_KEY",
                     ("MiniMax-M3",),
                     ("bearer",)),
        ),
    },
}

# The regions MiniMax answers on. The international one is first; the China
# one is here so a node behind that border is not told "unsupported".
_MINIMAX_ALT = Endpoint("minimax", ANTHROPIC, "https://api.minimaxi.com/anthropic",
                        "MINIMAX_API_KEY", ("MiniMax-M3",), ("bearer",))

PROVIDER_CHOICES = ("anthropic", "openai", "openrouter", "minimax")


# --------------------------------------------------------------- the prefixes --

# The format table. Longest prefix first — `sk-ant-` and `sk-or-` both begin
# with `sk-`, and `sk-` is the catch-all. The match is a prefix match on the
# value the operator pasted.
PREFIX_TABLE = (
    ("sk-ant-", "anthropic"),
    ("sk-or-", "openrouter"),
    ("sk-cp-", "minimax"),
    ("sk-proj-", "openai"),
    ("sk-", "openai"),
)

# `sk-cp-` reads MiniMax, but the bucket it belongs to is the OpenRouter /
# MiniMax family: a key in that family is asked at both places, in that
# order, and the gateway decides. The measured reason is in the module
# docstring — an `sk-cp-` key is refused by OpenRouter with a message that
# says so, which is exactly the kind of answer a probe should read rather
# than guess at.
FAMILY_CANDIDATES = {
    "minimax": ("minimax", "openrouter"),
}


@dataclass
class Guess:
    """What the format says, and where that leaves the probe."""
    provider: str
    prefix: str
    candidates: tuple
    source: str = "format"        # "format" | "override" | "unknown"
    env_var: str = ""
    note: str = ""

    @property
    def known(self) -> bool:
        return bool(self.candidates)


# ------------------------------------------------------------------- sniffing --

def _prefix_of(key: str) -> Optional[str]:
    for prefix, _provider in PREFIX_TABLE:
        if key.startswith(prefix):
            return prefix
    return None


def sniff(key: str) -> Guess:
    """Read the key's format and produce the endpoints worth asking.

    The result is a *shortlist*, not an answer. `sniff("sk-ant-...")` says
    "ask Anthropic"; it does not say the key works, and it does not say the
    key is Anthropic's. `probe()` is what settles it.

    A key whose format matches nothing returns a guess with no candidates
    and `source="unknown"` — the operator is asked which provider it is,
    because guessing at a key we cannot place is how the wrong variable name
    gets written in the first place.
    """
    key = (key or "").strip()
    prefix = _prefix_of(key)
    if prefix is None:
        return Guess(provider="", prefix="", candidates=(), source="unknown",
                     note="the format of this key matches no known provider")
    provider = dict(PREFIX_TABLE)[prefix]
    candidates = _candidates_for(provider)
    return Guess(provider=provider, prefix=prefix, candidates=candidates,
                 source="format",
                 env_var=PROVIDERS[provider]["endpoints"][0].env_var)


def _candidates_for(provider: str) -> tuple:
    """The endpoints to ask, in order, for a provider (or a family)."""
    family = FAMILY_CANDIDATES.get(provider, (provider,))
    out = []
    for name in family:
        for endpoint in PROVIDERS[name]["endpoints"]:
            out.append(endpoint)
    if provider == "minimax":
        out.append(_MINIMAX_ALT)
    return tuple(out)


def detect(key: str, override: Optional[str] = None) -> Guess:
    """The provider this key belongs to, with the operator's override honoured.

    An override is not a guess and not a fallback — it is the operator saying
    *I know where this key came from*. It wins over the format, and the
    candidate list becomes exactly that provider's endpoints.

    An override for a provider that does not exist is a usage error the
    caller turns into exit 2; this function raises ValueError.
    """
    if override:
        override = override.strip().lower()
        if override not in PROVIDERS:
            raise ValueError("unknown provider: " + override)
        endpoints = PROVIDERS[override]["endpoints"]
        if override == "minimax":
            endpoints = endpoints + (_MINIMAX_ALT,)
        return Guess(provider=override, prefix="", candidates=endpoints,
                     source="override", env_var=endpoints[0].env_var,
                     note="the operator named this provider")
    return sniff(key)


# ------------------------------------------------------------------- probing --

@dataclass
class ProbeResult:
    """What one endpoint said."""
    endpoint: Endpoint
    ok: bool
    status: Optional[int] = None
    shape: Optional[str] = None
    latency_ms: Optional[int] = None
    detail: str = ""
    auth_style: str = ""

    def line(self) -> str:
        mark = "✓" if self.ok else "✗"
        status = self.status if self.status is not None else "---"
        return f"  {mark} {self.endpoint.provider:<10} {self.endpoint.shape:<9} " \
               f"{status}  {self.endpoint.url}"


def _post(url: str, headers: dict, body: dict, timeout: float) -> tuple:
    """POST, and return (status, parsed_body, latency_ms)."""
    request = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), headers=headers,
        method="POST")
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
            status = response.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        status = exc.code
        exc.close()
    latency = int((time.monotonic() - started) * 1000)
    try:
        parsed = json.loads(raw)
    except Exception:
        parsed = None
    return status, parsed, latency, raw


def _body_shape(parsed) -> Optional[str]:
    """The shape that proves a model answered. Not the status code."""
    if not isinstance(parsed, dict):
        return None
    if parsed.get("type") == "message":
        return ANTHROPIC
    choices = parsed.get("choices")
    if isinstance(choices, list) and choices:
        return OPENAI
    return None


def _error_text(raw: str) -> str:
    """The provider's own words about why it said no."""
    try:
        parsed = json.loads(raw)
    except Exception:
        return raw[:160].strip()
    if isinstance(parsed, dict):
        err = parsed.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err.get("type") or "")[:200]
        if isinstance(err, str):
            return err[:200]
        if parsed.get("message"):
            return str(parsed["message"])[:200]
    return raw[:160].strip()


def probe(key: str, endpoint: Endpoint, model: Optional[str] = None,
          timeout: float = 20.0) -> ProbeResult:
    """Ask one endpoint whether it recognises this key.

    The assertion is on the **body**: an Anthropic `"type": "message"` or an
    OpenAI `"choices"` array. A 200 with the wrong body is not proof, and a
    401 is not "reachable" — it is the key being refused, which is a
    different failure with a different fix.

    The credential travels in a header, never in a URL, never on a command
    line, and never into the result. `detail` carries the provider's own
    error text, which is safe: it is the provider describing the key, not
    the key.
    """
    models = [model] if model else list(endpoint.models)
    last: Optional[ProbeResult] = None
    for candidate_model in models:
        for style in endpoint.auth:
            body = {"model": candidate_model, "max_tokens": 8,
                    "messages": [{"role": "user", "content": "ping"}]}
            try:
                status, parsed, latency, raw = _post(
                    endpoint.url, endpoint.headers(key, style), body, timeout)
            except Exception as exc:
                last = ProbeResult(endpoint, False, None, None, None,
                                   f"{type(exc).__name__}: {exc}", style)
                continue
            shape = _body_shape(parsed)
            if status == 200 and shape:
                return ProbeResult(endpoint, True, status, shape, latency,
                                   "", style)
            detail = _error_text(raw)
            if status in (401, 403):
                # The key was refused. Another model will not help, but
                # another *auth style* might — MiniMax documents Bearer and
                # OpenRouter accepts either.
                last = ProbeResult(endpoint, False, status, None, latency,
                                   detail, style)
                continue
            if status == 404:
                last = ProbeResult(endpoint, False, status, None, latency,
                                   "no such route on this base", style)
                break
            if status == 400 and "model" in detail.lower():
                # The key got far enough to be read; the model is the
                # problem. Try the next model.
                last = ProbeResult(endpoint, False, status, None, latency,
                                   detail, style)
                continue
            last = ProbeResult(endpoint, False, status, shape, latency,
                               detail, style)
            break
    return last or ProbeResult(endpoint, False, None, None, None,
                                "no answer", "")


def probe_candidates(key: str, candidates: Sequence[Endpoint],
                     model: Optional[str] = None, timeout: float = 20.0):
    """Ask each candidate in turn. The first one that answers wins."""
    results = []
    for endpoint in candidates:
        result = probe(key, endpoint, model=model, timeout=timeout)
        results.append(result)
        if result.ok:
            break
    return results


# ------------------------------------------------------------------ hygiene --

def fingerprint(key: str) -> str:
    """A one-way digest of the key. Twelve hex characters.

    It is not reversible and it is not a secret. It exists so a machine can
    be recognised as already configured, a receipt can be bound to the key
    it was written for, and the operator can be told *which* key is installed
    without ever seeing it.
    """
    return hashlib.sha256((key or "").encode("utf-8")).hexdigest()[:12]


# Anything that looks like a credential is masked in every output. The rule
# is deliberately broad: a false positive costs a little noise, and a false
# negative publishes a key.
_CREDENTIAL_RE = re.compile(
    r"(sk-ant-[A-Za-z0-9_\-]{4,}"
    r"|sk-or-[A-Za-z0-9_\-]{4,}"
    r"|sk-cp-[A-Za-z0-9_\-]{4,}"
    r"|sk-proj-[A-Za-z0-9_\-]{4,}"
    r"|sk-[A-Za-z0-9]{16,}"
    r"|tskey-[A-Za-z0-9_\-]{4,})")

# `NAME=value` where NAME looks like a secret. This is the leak the
# visibility lint calls out: an assignment line carries the value with it.
_ASSIGNMENT_RE = re.compile(
    r"\b([A-Z][A-Z0-9_]*(?:API_KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL))"
    r"(\s*=\s*)(\S+)")


def mask(text: str) -> str:
    """Replace anything credential-shaped with `***`.

    Every output the wizard produces goes through this. The wizard's output
    is an artifact that gets pasted into tickets, logs and chat, so it is
    treated as public by default.
    """
    if not text:
        return text
    out = _CREDENTIAL_RE.sub("***", text)
    out = _ASSIGNMENT_RE.sub(lambda m: m.group(1) + m.group(2) + "***", out)
    return out
