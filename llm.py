"""LLM providers, with a local-first chain that fails over to Claude.

The Pi runs the schedule; LM Studio runs on the Mac. When the Mac is asleep or
off the LAN, the local endpoint is simply unreachable — so the chain retries it
a configurable number of times (default 5) and then falls back to the Anthropic
API so the 3-hour cycle still produces scores.

Two failure classes matter and are deliberately distinguished:

  ProviderUnavailable — could not reach the provider at all. Retryable, and
                        the trigger for failing over to the fallback.
  ProviderFailed      — the provider answered but the answer was unusable.
                        Not a connectivity problem, so it does not fail over;
                        failing over would just spend money on the same bad
                        input.

Measured on this hardware (see README): qwen3.6-35b-a3b answers in 7-13s with
reliable schema-valid JSON. gemma-4-26b-a4b-qat truncates mid-string on longer
German descriptions and is ~5x slower, so it is not a viable primary.
"""

import json
import logging
import os
import re
import time

import requests

log = logging.getLogger(__name__)

DEFAULT_LOCAL_BASE_URL = "http://localhost:1234/v1"
DEFAULT_LOCAL_MODEL = "qwen/qwen3.6-35b-a3b"

# The Anthropic base URL is pinned rather than left to the SDK's environment
# lookup: ANTHROPIC_BASE_URL is set to the local LM Studio endpoint in this
# user's Claude Code config, and inheriting it would silently send "fallback"
# traffic straight back to the machine we just failed to reach.
ANTHROPIC_BASE_URL = "https://api.anthropic.com"
DEFAULT_ANTHROPIC_MODEL = "claude-opus-5"

# Shown on the config page so the cost of the fallback is an informed choice.
ANTHROPIC_MODELS = [
    ("claude-opus-5", "Opus 5 — $5/$25 per Mtok (~$0.015/job)"),
    ("claude-sonnet-5", "Sonnet 5 — $3/$15 per Mtok (~$0.009/job)"),
    ("claude-haiku-4-5", "Haiku 4.5 — $1/$5 per Mtok (~$0.003/job)"),
]

DEFAULT_ATTEMPTS = 5
DEFAULT_BACKOFF = 2.0
DEFAULT_TIMEOUT = 180

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL)
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


class LLMError(Exception):
    """Base class for provider problems."""


class ProviderUnavailable(LLMError):
    """Could not reach the provider. Retryable; triggers failover."""


class ProviderFailed(LLMError):
    """Provider responded, but not usably. Does not trigger failover."""


def clean_json_text(text):
    """Strip reasoning wrappers and code fences from a model's JSON reply."""
    if not text:
        return ""
    text = _THINK_BLOCK.sub("", text).strip()
    text = _FENCE.sub("", text).strip()
    return text


class Provider(object):
    name = "base"
    label = "Base"
    model = ""

    def model_label(self):
        """Identifier recorded on each score, e.g. 'local:qwen/qwen3.6-35b-a3b'."""
        return "{0}:{1}".format(self.name, self.model)

    def complete(self, system, user, json_schema=None, max_tokens=1500,
                 temperature=0.1):
        """Return the model's reply as a string."""
        raise NotImplementedError

    def complete_json(self, system, user, json_schema, max_tokens=1500,
                      temperature=0.1):
        """Return a parsed dict, repairing one bad reply before giving up."""
        raw = self.complete(system, user, json_schema, max_tokens, temperature)
        cleaned = clean_json_text(raw)
        try:
            return json.loads(cleaned)
        except ValueError:
            log.warning("%s returned unparseable JSON; retrying once", self.name)

        # One repair attempt: hand the broken output back and ask for JSON only.
        repair_user = (
            "Your previous reply was not valid JSON. Return the same content as a "
            "single valid JSON object matching the schema. Output only the JSON — "
            "no prose, no code fences.\n\nPrevious reply:\n" + (raw or "")[:4000]
        )
        raw2 = self.complete(system, repair_user, json_schema, max_tokens, temperature)
        cleaned2 = clean_json_text(raw2)
        try:
            return json.loads(cleaned2)
        except ValueError as exc:
            raise ProviderFailed(
                "{0} produced invalid JSON twice: {1}".format(self.name, exc)
            )

    def list_models(self):
        return []

    def test(self):
        """Return (ok, message) for the config page's Test button."""
        raise NotImplementedError


class OpenAICompatProvider(Provider):
    """LM Studio, Ollama, or anything else speaking the OpenAI chat API."""

    name = "local"
    label = "Local (LM Studio / OpenAI-compatible)"

    def __init__(self, base_url=DEFAULT_LOCAL_BASE_URL, model=DEFAULT_LOCAL_MODEL,
                 api_key=None, attempts=DEFAULT_ATTEMPTS, backoff=DEFAULT_BACKOFF,
                 timeout=DEFAULT_TIMEOUT):
        self.base_url = (base_url or DEFAULT_LOCAL_BASE_URL).rstrip("/")
        self.model = model or DEFAULT_LOCAL_MODEL
        self.api_key = api_key
        self.attempts = max(1, int(attempts))
        self.backoff = float(backoff)
        self.timeout = int(timeout)

    def _headers(self):
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer {0}".format(self.api_key)
        return headers

    def complete(self, system, user, json_schema=None, max_tokens=1500,
                 temperature=0.1):
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_schema:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "result", "strict": True, "schema": json_schema},
            }

        payload = self._post_with_retries("/chat/completions", body)
        try:
            message = payload["choices"][0]["message"]
        except (KeyError, IndexError):
            raise ProviderFailed("Unexpected response shape from {0}".format(self.base_url))
        return _extract_message_text(message)

    def _post_with_retries(self, path, body):
        """Retry connectivity failures; surface HTTP errors immediately.

        The Mac being asleep looks like a connection error, which is exactly
        the case worth retrying. An HTTP 400 (bad schema, model not loaded)
        will not fix itself, so it fails fast instead of burning the budget.
        """
        url = self.base_url + path
        last_error = None
        for attempt in range(1, self.attempts + 1):
            try:
                resp = requests.post(
                    url, json=body, headers=self._headers(), timeout=self.timeout
                )
            except requests.RequestException as exc:
                last_error = exc
                if attempt < self.attempts:
                    delay = self.backoff * attempt
                    log.warning(
                        "local LLM unreachable (attempt %d/%d): %s — retrying in %.0fs",
                        attempt, self.attempts, exc, delay,
                    )
                    time.sleep(delay)
                continue

            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError:
                    raise ProviderFailed("Local LLM returned a non-JSON body")

            detail = _error_detail(resp)
            # A model still warming up is worth another attempt; other 4xx
            # responses are configuration problems and will not improve.
            if resp.status_code >= 500 or _is_transient(detail):
                last_error = RuntimeError("HTTP {0}: {1}".format(resp.status_code, detail))
                if attempt < self.attempts:
                    time.sleep(self.backoff * attempt)
                continue
            raise ProviderFailed(
                "Local LLM returned HTTP {0}: {1}".format(resp.status_code, detail)
            )

        raise ProviderUnavailable(
            "Local LLM at {0} unreachable after {1} attempts: {2}".format(
                self.base_url, self.attempts, last_error
            )
        )

    def list_models(self):
        try:
            resp = requests.get(
                self.base_url + "/models", headers=self._headers(), timeout=15
            )
        except requests.RequestException as exc:
            raise ProviderUnavailable("Cannot reach {0}: {1}".format(self.base_url, exc))
        if resp.status_code != 200:
            raise ProviderFailed("HTTP {0} from {1}/models".format(resp.status_code, self.base_url))
        try:
            return [m["id"] for m in resp.json().get("data", [])]
        except (ValueError, KeyError, TypeError):
            raise ProviderFailed("Unexpected /models response")

    def test(self):
        try:
            models = self.list_models()
        except LLMError as exc:
            return False, str(exc)
        if self.model not in models:
            return False, (
                "Reachable, but '{0}' is not loaded. Available: {1}".format(
                    self.model, ", ".join(models[:6]) or "none"
                )
            )
        return True, "OK — {0} loaded, {1} model(s) available".format(
            self.model, len(models)
        )


def _extract_message_text(message):
    """Read the reply, tolerating thinking models.

    LM Studio puts a thinking model's output in `reasoning_content` and leaves
    `content` empty — qwen3.6 does exactly this, and reading only `content`
    makes a perfectly good reply look like a blank response.
    """
    text = (message.get("content") or "").strip()
    if not text:
        text = (message.get("reasoning_content") or "").strip()
    return text


# Substrings that mean "this will not fix itself" — a human has to act. These
# are checked first, because LM Studio's out-of-memory message also contains
# the word "loading" ("Model loading was stopped due to insufficient system
# resources"), which would otherwise look transient and burn all five retries.
_PERMANENT_MARKERS = (
    "insufficient system resources",
    "loading was stopped",
    "not found",
    "no models loaded",
    "unknown model",
)
_TRANSIENT_MARKERS = ("still loading", "is loading", "warming up", "please wait")


def _is_transient(detail):
    text = (detail or "").lower()
    if any(marker in text for marker in _PERMANENT_MARKERS):
        return False
    return any(marker in text for marker in _TRANSIENT_MARKERS)


def _error_detail(resp):
    try:
        return str(resp.json().get("error", {}).get("message", ""))[:300]
    except (ValueError, AttributeError):
        return resp.text[:300]


class AnthropicProvider(Provider):
    """Claude via the Anthropic Messages API."""

    name = "anthropic"
    label = "Anthropic (Claude)"

    def __init__(self, api_key, model=DEFAULT_ANTHROPIC_MODEL, timeout=DEFAULT_TIMEOUT):
        self.api_key = api_key
        self.model = model or DEFAULT_ANTHROPIC_MODEL
        self.timeout = int(timeout)
        self._client = None

    def _get_client(self):
        if self._client is None:
            if not self.api_key:
                raise ProviderFailed("No Anthropic API key configured")
            import anthropic

            # base_url is pinned explicitly — see ANTHROPIC_BASE_URL above.
            self._client = anthropic.Anthropic(
                api_key=self.api_key,
                base_url=ANTHROPIC_BASE_URL,
                timeout=float(self.timeout),
            )
        return self._client

    def complete(self, system, user, json_schema=None, max_tokens=1500,
                 temperature=0.1):
        import anthropic

        client = self._get_client()
        kwargs = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        if json_schema:
            kwargs["output_config"] = {
                "format": {"type": "json_schema", "schema": json_schema}
            }
        try:
            response = client.messages.create(**kwargs)
        except anthropic.APIConnectionError as exc:
            raise ProviderUnavailable("Cannot reach the Anthropic API: {0}".format(exc))
        except anthropic.AuthenticationError:
            raise ProviderFailed("Anthropic rejected the API key")
        except anthropic.RateLimitError as exc:
            raise ProviderUnavailable("Anthropic rate limited: {0}".format(exc))
        except anthropic.APIStatusError as exc:
            if exc.status_code >= 500:
                raise ProviderUnavailable("Anthropic server error {0}".format(exc.status_code))
            raise ProviderFailed("Anthropic error {0}: {1}".format(exc.status_code, exc.message))

        if getattr(response, "stop_reason", None) == "refusal":
            raise ProviderFailed("Claude declined to answer this request")
        return "".join(
            block.text for block in response.content if getattr(block, "type", "") == "text"
        )

    def list_models(self):
        return [model_id for model_id, _ in ANTHROPIC_MODELS]

    def test(self):
        import anthropic

        try:
            client = self._get_client()
            client.messages.create(
                model=self.model,
                max_tokens=16,
                messages=[{"role": "user", "content": "Reply with the single word OK."}],
            )
        except anthropic.AuthenticationError:
            return False, "API key rejected"
        except ProviderFailed as exc:
            return False, str(exc)
        except Exception as exc:
            return False, "{0}: {1}".format(type(exc).__name__, exc)
        return True, "OK — {0} reachable".format(self.model)


class FallbackProvider(Provider):
    """Try the primary; fail over to the secondary when it is unreachable.

    Only ProviderUnavailable triggers failover. A ProviderFailed means the
    primary answered badly — retrying the same input on a paid API would spend
    money to get the same result.
    """

    name = "fallback"
    label = "Local with Claude fallback"

    def __init__(self, primary, secondary):
        self.primary = primary
        self.secondary = secondary
        #: model_label() of whichever provider served the most recent call, so
        #: each score records where it actually came from.
        self.last_used = None

    def model_label(self):
        return self.last_used or "fallback:unused"

    def complete(self, system, user, json_schema=None, max_tokens=1500,
                 temperature=0.1):
        try:
            result = self.primary.complete(system, user, json_schema, max_tokens, temperature)
            self.last_used = self.primary.model_label()
            return result
        except ProviderUnavailable as exc:
            if self.secondary is None:
                raise
            log.warning("primary provider unavailable (%s) — falling back to %s",
                        exc, self.secondary.name)
        result = self.secondary.complete(system, user, json_schema, max_tokens, temperature)
        self.last_used = self.secondary.model_label()
        return result

    def test(self):
        ok, message = self.primary.test()
        if ok:
            return True, "Primary: " + message
        if self.secondary is None:
            return False, "Primary unavailable ({0}) and no fallback configured".format(message)
        ok2, message2 = self.secondary.test()
        if ok2:
            return True, "Primary unavailable ({0}); fallback ready: {1}".format(message, message2)
        return False, "Primary: {0} | Fallback: {1}".format(message, message2)


def build_provider(settings, secrets=None):
    """Assemble the configured provider chain from settings.

    `settings` is a plain dict of the llm.* keys; `secrets` supplies the API
    key. Environment variables win over stored values so the Pi's systemd unit
    can hold the key without it ever entering the database.
    """
    secrets = secrets or {}

    local = OpenAICompatProvider(
        base_url=settings.get("llm.local.base_url", DEFAULT_LOCAL_BASE_URL),
        model=settings.get("llm.local.model", DEFAULT_LOCAL_MODEL),
        api_key=settings.get("llm.local.api_key") or None,
        attempts=settings.get("llm.local.attempts", DEFAULT_ATTEMPTS),
        backoff=settings.get("llm.local.backoff", DEFAULT_BACKOFF),
        timeout=settings.get("llm.timeout", DEFAULT_TIMEOUT),
    )

    api_key = os.environ.get("ANTHROPIC_API_KEY") or secrets.get("anthropic.api_key")
    fallback_enabled = settings.get("llm.fallback.enabled", True)
    if not (api_key and fallback_enabled):
        return local

    claude = AnthropicProvider(
        api_key=api_key,
        model=settings.get("llm.anthropic.model", DEFAULT_ANTHROPIC_MODEL),
        timeout=settings.get("llm.timeout", DEFAULT_TIMEOUT),
    )
    return FallbackProvider(local, claude)
