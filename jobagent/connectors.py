"""LLM connectors, configured only via environment variables – no endpoints or keys in the code.

Every connector speaks the OpenAI-compatible chat API (LM Studio, OpenAI, Ollama, vLLM, …). Connectors are tried in
the order given by LLM_CONNECTORS. Per connector NAME (upper-cased in the variable names):

    LLM_CONNECTORS=lmstudio                     # comma-separated, first available wins; empty = no AI processing
    LLM_<NAME>_BASE_URL=https://…/v1            # required (defaults to https://api.openai.com/v1 for NAME=openai)
    LLM_<NAME>_API_KEY=…                        # optional bearer token (LM Studio API token, OpenAI key)
    LLM_<NAME>_MODEL=…                          # optional; empty = whatever model the server has loaded
    LLM_<NAME>_HEADERS={"CF-Access-Client-Id": "…", "CF-Access-Client-Secret": "…"}   # optional extra headers (JSON)
    LLM_<NAME>_TIMEOUT=180                      # seconds per request
"""

import json
import logging
import os
import re

import requests

log = logging.getLogger("webzap-jobs.llm")


class Unavailable(Exception):
    """The connector can't be reached right now – retry later, this is not the task's fault."""


class BadOutput(Exception):
    """The model answered, but not with usable JSON."""


class Connector:
    def __init__(self, name: str):
        def env(key: str, default: str = "") -> str:
            return os.environ.get(f"LLM_{name.upper()}_{key}", default).strip()

        self.name = name
        self.base_url = env("BASE_URL", "https://api.openai.com/v1" if name == "openai" else "").rstrip("/")
        self.model = env("MODEL")
        self.timeout = float(env("TIMEOUT", "180"))
        self.headers = {"Content-Type": "application/json"}
        if env("API_KEY"):
            self.headers["Authorization"] = f"Bearer {env('API_KEY')}"
        if env("HEADERS"):
            self.headers.update(json.loads(env("HEADERS")))
        if not self.base_url:
            raise ValueError(f"LLM_{name.upper()}_BASE_URL is not set")
        self._model_seen = ""

    def __repr__(self):  # never print URLs or keys
        return f"<Connector {self.name}>"

    def available(self) -> str | None:
        """Return the model id to use, or None if the server is offline or has no model loaded."""
        try:
            r = requests.get(f"{self.base_url}/models", headers=self.headers, timeout=10)
            if r.status_code != 200:
                log.info("connector %s: /models returned %s", self.name, r.status_code)
                return None
            ids = [m.get("id") for m in r.json().get("data", []) if m.get("id")]
        except (requests.RequestException, ValueError) as e:
            log.info("connector %s offline: %s", self.name, e.__class__.__name__)
            return None
        if self.model:
            return self.model if (self.model in ids or self.name == "openai") else None
        # LM Studio lists loaded models; skip embedding models.
        ids = [i for i in ids if "embed" not in i.lower()]
        self._model_seen = ids[0] if ids else ""
        return self._model_seen or None

    def chat_json(self, model: str, system: str, user: str, schema: dict) -> dict:
        """One chat call that must return a JSON object. Tries structured output first, then plain JSON prompting."""
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        attempts = [{"type": "json_schema", "json_schema": {"name": "result", "schema": schema}}, None]
        last = None
        for fmt in attempts:
            # Streamed, so proxies with idle timeouts (Cloudflare: ~100 s) see bytes flowing during long generations.
            body = {"model": model, "messages": messages, "stream": True}
            if fmt:
                body["response_format"] = fmt
            try:
                r = requests.post(
                    f"{self.base_url}/chat/completions",
                    headers=self.headers,
                    json=body,
                    timeout=(15, self.timeout),
                    stream=True,
                )
            except requests.RequestException as e:
                raise Unavailable(f"{self.name}: {e.__class__.__name__}") from e
            with r:
                if r.status_code in (502, 503, 504, 521, 522, 523, 524, 530):  # server/tunnel down or overloaded
                    raise Unavailable(f"{self.name}: HTTP {r.status_code}")
                if r.status_code == 400 and fmt:  # server doesn't support json_schema → plain prompting
                    last = f"HTTP 400 with json_schema: {r.text[:200]}"
                    continue
                if r.status_code != 200:
                    raise BadOutput(f"{self.name}: HTTP {r.status_code}: {r.text[:200]}")
                try:
                    content = _read_stream(r)
                except requests.RequestException as e:
                    raise Unavailable(f"{self.name}: stream broke ({e.__class__.__name__})") from e
            try:
                return parse_json(content)
            except ValueError as e:
                last = str(e)
        raise BadOutput(f"{self.name}: no JSON in answer ({last})")


def _read_stream(r) -> str:
    """Collect the content of an OpenAI-style SSE stream (also accepts a non-streamed JSON answer)."""
    r.encoding = "utf-8"  # SSE responses often omit the charset; requests would fall back to Latin-1
    if "text/event-stream" not in r.headers.get("content-type", ""):
        return r.json()["choices"][0]["message"].get("content") or ""
    parts = []
    for line in r.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            delta = json.loads(data)["choices"][0].get("delta", {})
        except (ValueError, KeyError, IndexError):
            continue
        parts.append(delta.get("content") or "")
    return "".join(parts)


def parse_json(text: str) -> dict:
    """Extract a JSON object from a model answer (handles <think> blocks and ``` fences)."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no JSON object found")
    obj = json.loads(text[start : end + 1])
    if not isinstance(obj, dict):
        raise ValueError("JSON is not an object")
    return obj


def configured() -> list[Connector]:
    names = [n.strip().lower() for n in os.environ.get("LLM_CONNECTORS", "").split(",") if n.strip()]
    out = []
    for n in names:
        try:
            out.append(Connector(n))
        except (ValueError, json.JSONDecodeError) as e:
            log.error("connector %s misconfigured: %s", n, e)
    return out


def pick(connectors: list[Connector]) -> tuple[Connector, str] | None:
    """First connector that is online with a model loaded."""
    for c in connectors:
        model = c.available()
        if model:
            return c, model
    return None


def backoff(n: int, base: float = 30, cap: float = 1800) -> float:
    """Wait time after the n-th consecutive offline check: 30 s, 60 s, 2 min … up to 30 min."""
    return min(cap, base * 2 ** max(0, n - 1))
