"""
The swappable brain.

Voice always runs on Gemini Live — it is the ears and the mouth. Everything
typed (Telegram), everything that runs on its own (jobs), and any hard
question voice hands over (ask_brain) goes to whichever brain config.json
names in "brain":

    gemini      Gemini's text API, with the same key voice uses
    claude      Anthropic's Claude — needs `pip install anthropic` and ANTHROPIC_API_KEY
    openrouter  any model OpenRouter serves (GPT, Grok, Llama...) — needs OPENROUTER_API_KEY

Every brain gets the same tools from tools.py, and switching keeps the memory,
because the memory is the Markdown brain, not the model.
"""

import importlib.util
import json
import os
import time

import requests

import tools
from tools import CFG, log

PROVIDERS = ("gemini", "claude", "openrouter")
DEFAULT_MODELS = {"claude": "claude-opus-5-5", "openrouter": "openrouter/auto"}
MAX_ROUNDS = 10           # tool calls per question before giving up
MAX_HISTORY = 60          # messages kept before a conversation starts fresh
# a brain asking itself would loop; and only he, by voice or a Telegram command, picks the brain —
# never a model that might have just read a web page telling it to
NOT_FOR_ENGINES = {"ask_brain", "use_brain"}

# Claude models that can retry on another model when a safety check declines
_FALLBACK_MODELS = ("claude-fable-5-2", "claude-fable-5-1", "claude-opus-5-5",
                    "claude-opus-5", "claude-sonnet-5-5")


class EngineError(Exception):
    pass


def current():
    name = (CFG.get("brain") or "gemini").lower()
    return name if name in PROVIDERS else "gemini"


def model_for(provider):
    chosen = (CFG.get("brain_models") or {}).get(provider)
    if provider == "gemini":
        return chosen or CFG.get("model") or "gemini-3.6-flash"
    return chosen or DEFAULT_MODELS[provider]


def missing_key(provider):
    """What stops this brain working, in words — or None if it is ready."""
    if provider == "gemini" and not CFG.get("api_key"):
        return "GEMINI_API_KEY is not set in .env"
    if provider == "openrouter" and not os.environ.get("OPENROUTER_API_KEY"):
        return "OPENROUTER_API_KEY is not set in .env"
    if provider == "claude":
        if importlib.util.find_spec("anthropic") is None:
            return "the Claude brain needs the anthropic package: .venv/bin/pip install anthropic"
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return "ANTHROPIC_API_KEY is not set in .env"
    return None


def json_schema(gemini_schema):
    """tools.declarations() is written in Gemini's dialect ("type": "STRING");
       Claude and OpenAI-style APIs want plain JSON Schema ("type": "string")."""
    if isinstance(gemini_schema, dict):
        return {k: (v.lower() if k == "type" and isinstance(v, str) else json_schema(v))
                for k, v in gemini_schema.items()}
    if isinstance(gemini_schema, list):
        return [json_schema(v) for v in gemini_schema]
    return gemini_schema


def _declarations():
    return [d for d in tools.declarations() if d["name"] not in NOT_FOR_ENGINES]


class Conversation:
    """One running conversation with the brain — a Telegram chat, a job, a
       question from voice. ask() blocks; call it off the event loop."""

    def __init__(self, channel, gate=None):
        self.channel = channel
        self.gate = gate              # None: anything needing a yes is refused
        self.provider = None
        self.history = []             # in the provider's own format, append-only

    def system(self):
        return (tools.build_system() +
                f"\n\nRight now you are answering in {self.channel}, in writing, not out loud. "
                "Keep it short and plain. Nobody can see your screen from here unless you describe it.")

    def ask(self, text):
        provider = current()
        problem = missing_key(provider)
        if problem:
            return f"My {provider} brain isn't ready: {problem}."
        if provider != self.provider or len(self.history) > MAX_HISTORY:
            # shortcut: a long chat starts over rather than being summarised; the brain folder keeps what matters
            self.provider, self.history = provider, []

        turn, add_results = {
            "gemini": (self._gemini_turn, self._gemini_results),
            "claude": (self._claude_turn, self._claude_results),
            "openrouter": (self._openrouter_turn, self._openrouter_results),
        }[provider]
        self._add_user(provider, text)
        system, decls = self.system(), _declarations()
        try:
            for _ in range(MAX_ROUNDS):
                reply, calls = turn(system, decls)
                if not calls:
                    return reply.strip() or "Done."
                allowed = {d["name"] for d in decls}
                add_results([(call_id, name, tools.run(name, args, self.gate) if name in allowed
                              else f"{name} is not available here")
                             for call_id, name, args in calls])
            return "I stopped there — that was taking too many steps. Ask again more specifically?"
        except Exception as e:
            # a half-finished tool exchange left in the history would be refused next time
            self.history = []
            log(f"{provider} brain failed:", e)
            return f"My {provider} brain hit a problem: {e}"

    def _add_user(self, provider, text):
        if provider == "gemini":
            self.history.append({"role": "user", "parts": [{"text": text}]})
        else:
            self.history.append({"role": "user", "content": text})

    # ---- Gemini ----

    def _gemini_turn(self, system, decls):
        body = {"systemInstruction": {"parts": [{"text": system}]},
                "contents": self.history,
                "tools": [{"functionDeclarations": decls}]}
        res = tools._gemini_post(body, model_for("gemini"), timeout=120)
        if res.status_code == 503:          # "high demand" — usually gone in seconds
            time.sleep(5)
            res = tools._gemini_post(body, model_for("gemini"), timeout=120)
        if not res.ok:
            raise EngineError(f"Gemini returned {res.status_code}: {res.text[:200]}")
        try:
            content = res.json()["candidates"][0]["content"]
        except (KeyError, IndexError, ValueError):
            raise EngineError("Gemini sent back nothing usable")
        self.history.append(content)      # whole, so its thought signatures go back unchanged
        parts = content.get("parts", [])
        calls = [(p["functionCall"].get("id"), p["functionCall"]["name"], p["functionCall"].get("args") or {})
                 for p in parts if "functionCall" in p]
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        return text, calls

    def _gemini_results(self, results):
        parts = []
        for call_id, name, result in results:
            response = {"name": name, "response": {"result": result}}
            if call_id:
                response["id"] = call_id
            parts.append({"functionResponse": response})
        self.history.append({"role": "user", "parts": parts})

    # ---- Claude ----

    def _claude_turn(self, system, decls):
        import anthropic
        model = model_for("claude")
        kwargs = {}
        if model.startswith(_FALLBACK_MODELS):
            # on a safety decline, the API retries on a suitable model itself
            kwargs = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
        response = anthropic.Anthropic().beta.messages.create(
            model=model,
            max_tokens=16000,
            system=system,
            messages=self.history,
            tools=[{"name": d["name"], "description": d["description"],
                    "input_schema": json_schema(d["parameters"])} for d in decls],
            output_config={"effort": CFG.get("claude_effort") or "medium"},
            **kwargs,
        )
        if response.stop_reason == "refusal":
            raise EngineError("Claude declined that one")
        self.history.append({"role": "assistant", "content": response.content})
        text = "".join(b.text for b in response.content if b.type == "text")
        calls = [(b.id, b.name, b.input) for b in response.content if b.type == "tool_use"]
        return text, calls

    def _claude_results(self, results):
        self.history.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": call_id, "content": result}
            for call_id, _, result in results]})

    # ---- OpenRouter (OpenAI-style) ----

    def _openrouter_turn(self, system, decls):
        res = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {os.environ.get('OPENROUTER_API_KEY', '')}"},
            json={"model": model_for("openrouter"),
                  "messages": [{"role": "system", "content": system}] + self.history,
                  "tools": [{"type": "function", "function": {
                      "name": d["name"], "description": d["description"],
                      "parameters": json_schema(d["parameters"])}} for d in decls]},
            timeout=180)
        if not res.ok:
            raise EngineError(f"OpenRouter returned {res.status_code}: {res.text[:200]}")
        try:
            message = res.json()["choices"][0]["message"]
        except (KeyError, IndexError, ValueError):
            raise EngineError("OpenRouter sent back nothing usable")
        self.history.append(message)
        calls = []
        for c in message.get("tool_calls") or []:
            try:
                args = json.loads(c["function"].get("arguments") or "{}")
            except ValueError:
                args = {}
            calls.append((c["id"], c["function"]["name"], args))
        return message.get("content") or "", calls

    def _openrouter_results(self, results):
        for call_id, _, result in results:
            self.history.append({"role": "tool", "tool_call_id": call_id, "content": result})


def ask_once(question, channel="a question passed over from voice"):
    """One question, one answer, no memory of the exchange beyond the brain folder."""
    return Conversation(channel).ask(question)
