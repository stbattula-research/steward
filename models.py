"""Model registry: every brain you've added, which one you're chatting with, and which
one runs scheduled tasks. API keys live in the macOS Keychain, never in a file.

Two kinds of providers:
  - "anthropic": speak the Claude Messages API (Claude, Ollama, OpenRouter, DeepSeek, ...)
  - "openai":    speak the OpenAI Chat Completions API (Gemini, OpenAI, Groq, Mistral, NVIDIA ...)
                 and are translated by the built-in bridge (bridge.py).
Every provider that needs a key is reached through the bridge, so keys never enter the
agent's own environment (which its shell commands could read).
"""
import json
import re
import shutil
import subprocess
import time
import uuid

import config

KEYCHAIN_SERVICE = "steward-model"

# Presets shown in the "Add model" form. `examples` are only model IDs taken from the
# providers' own docs; for the rest, the form links to the provider's model list.
PROVIDERS = {
    "ollama": {"label": "Ollama (on this Mac)", "group": "On this Mac", "kind": "anthropic",
               "base": "http://localhost:11434", "key": False, "local": True, "ctx": 65536,
               "help": "Free and private. Any model you've downloaded with Ollama.",
               "models_url": "https://ollama.com/search?c=tools"},
    "anthropic": {"label": "Claude (Anthropic)", "group": "Claude-compatible", "kind": "anthropic",
                  "base": "https://api.anthropic.com", "key": True,
                  "help": "Strongest at long, multi-step tasks. Leave Model ID blank for the default.",
                  "keys_url": "https://console.anthropic.com/settings/keys",
                  "models_url": "https://docs.claude.com/en/docs/about-claude/models"},
    "openrouter": {"label": "OpenRouter", "group": "Claude-compatible", "kind": "anthropic",
                   "base": "https://openrouter.ai/api", "key": True,
                   "help": "One key for hundreds of models. Free models have small daily limits.",
                   "keys_url": "https://openrouter.ai/settings/keys", "models_url": "https://openrouter.ai/models"},
    "deepseek": {"label": "DeepSeek", "group": "Claude-compatible", "kind": "anthropic",
                 "base": "https://api.deepseek.com/anthropic", "key": True,
                 "examples": ["deepseek-flash", "deepseek-v4-pro"],
                 "keys_url": "https://platform.deepseek.com/api_keys",
                 "models_url": "https://api-docs.deepseek.com/quick_start/pricing"},
    "ollama-cloud": {"label": "Ollama Cloud", "group": "Claude-compatible", "kind": "anthropic",
                     "base": "https://ollama.com", "key": True,
                     "help": "Large open models hosted by Ollama.",
                     "keys_url": "https://ollama.com/settings/keys", "models_url": "https://ollama.com/search?c=cloud"},
    "gemini": {"label": "Google Gemini", "group": "OpenAI-compatible", "kind": "openai",
               "base": "https://generativelanguage.googleapis.com/v1beta/openai", "key": True,
               "examples": ["gemini-3.8-flash"],
               "help": "Free tier available (Google may use free-tier data to improve its products).",
               "keys_url": "https://aistudio.google.com/apikey",
               "models_url": "https://ai.google.dev/gemini-api/docs/models"},
    "openai": {"label": "OpenAI", "group": "OpenAI-compatible", "kind": "openai",
               "base": "https://api.openai.com/v1", "key": True,
               "keys_url": "https://platform.openai.com/api-keys",
               "models_url": "https://platform.openai.com/docs/models"},
    "groq": {"label": "Groq", "group": "OpenAI-compatible", "kind": "openai",
             "base": "https://api.groq.com/openai/v1", "key": True,
             "keys_url": "https://console.groq.com/keys", "models_url": "https://console.groq.com/docs/models"},
    "mistral": {"label": "Mistral", "group": "OpenAI-compatible", "kind": "openai",
                "base": "https://api.mistral.ai/v1", "key": True,
                "keys_url": "https://console.mistral.ai/api-keys",
                "models_url": "https://docs.mistral.ai/getting-started/models/"},
    "nvidia": {"label": "NVIDIA NIM", "group": "OpenAI-compatible", "kind": "openai",
               "base": "https://integrate.api.nvidia.com/v1", "key": True,
               "keys_url": "https://build.nvidia.com", "models_url": "https://build.nvidia.com/models"},
    "custom-anthropic": {"label": "Other (Claude/Anthropic format)", "group": "Other", "kind": "anthropic",
                         "base": "", "key": True, "help": "Any endpoint that speaks the Anthropic Messages API."},
    "custom-openai": {"label": "Other (OpenAI format)", "group": "Other", "kind": "openai",
                      "base": "", "key": True, "help": "Any endpoint that speaks OpenAI Chat Completions."},
}


# ------------------------------------------------------------------ keys --
def _has_keychain() -> bool:
    return shutil.which("security") is not None


_FALLBACK = config.STATE_DIR / "model_keys.json"   # only used off macOS (development)


def _fallback_load() -> dict:
    try:
        return json.loads(_FALLBACK.read_text())
    except Exception:
        return {}


def set_key(model_id: str, key: str) -> None:
    if _has_keychain():
        subprocess.run(["security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE, "-a", model_id,
                        "-l", f"Steward model - {model_id}", "-w", key], check=True, capture_output=True)
    else:
        data = _fallback_load(); data[model_id] = key
        _FALLBACK.write_text(json.dumps(data)); _FALLBACK.chmod(0o600)


def get_key(model_id: str) -> str:
    if _has_keychain():
        r = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", model_id, "-w"],
                           capture_output=True, text=True)
        return r.stdout.rstrip("\n") if r.returncode == 0 else ""
    return _fallback_load().get(model_id, "")


def delete_key(model_id: str) -> None:
    if _has_keychain():
        subprocess.run(["security", "delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", model_id],
                       capture_output=True)
    else:
        data = _fallback_load(); data.pop(model_id, None)
        _FALLBACK.write_text(json.dumps(data))


# ------------------------------------------------------------- registry --
class Registry:
    FILE = config.STATE_DIR / "models.json"

    def __init__(self):
        self.models: list[dict] = []
        self.active = ""        # used for chat
        self.background = ""    # used for scheduled tasks and heads-ups
        self._key_cache: dict[str, bool] = {}
        self.load()

    # ---- persistence
    def load(self) -> None:
        if self.FILE.exists():
            data = json.loads(self.FILE.read_text())
            self.models, self.active, self.background = data["models"], data.get("active", ""), data.get("background", "")
        if not self.models and config.BRAIN_PROVIDER != "none":
            self._seed_from_env()
        ids = [m["id"] for m in self.models]
        if self.active not in ids:
            self.active = ids[0] if ids else ""
        if self.background not in ids:
            self.background = self.active
        self.save()

    def save(self) -> None:
        self.FILE.write_text(json.dumps({"models": self.models, "active": self.active,
                                         "background": self.background}, indent=2))
        self.FILE.chmod(0o600)

    def _seed_from_env(self) -> None:
        """First run: turn the brain chosen in the setup wizard into the first model."""
        p = config.BRAIN_PROVIDER
        if p == "anthropic":
            m = self._new("Claude", "anthropic", config.CLAUDE_MODEL or "")
            if config.ANTHROPIC_API_KEY:
                set_key(m["id"], config.ANTHROPIC_API_KEY)
        elif p in ("ollama-cloud", "custom"):
            prov = "ollama-cloud" if p == "ollama-cloud" else "custom-anthropic"
            m = self._new(config.BRAIN_MODEL or PROVIDERS[prov]["label"], prov, config.BRAIN_MODEL,
                          base_url=config.BRAIN_BASE_URL, context_tokens=config.BRAIN_CONTEXT_TOKENS)
            if config.BRAIN_API_KEY:
                set_key(m["id"], config.BRAIN_API_KEY)
        else:
            m = self._new(config.OLLAMA_MODEL, "ollama", config.OLLAMA_MODEL,
                          context_tokens=config.OLLAMA_CONTEXT)
        self.models.append(m)
        self.active = self.background = m["id"]

    @staticmethod
    def _new(label, provider, model, base_url="", context_tokens=0) -> dict:
        return {"id": re.sub(r"[^a-z0-9]+", "-", (label or provider).lower()).strip("-")[:24] + "-" + uuid.uuid4().hex[:4],
                "label": label or PROVIDERS[provider]["label"], "provider": provider, "model": model or "",
                "base_url": base_url or "", "context_tokens": int(context_tokens or 0), "created": int(time.time())}

    # ---- queries
    def get(self, model_id: str) -> dict | None:
        return next((m for m in self.models if m["id"] == model_id), None)

    def active_model(self) -> dict | None:
        return self.get(self.active) or (self.models[0] if self.models else None)

    def background_model(self) -> dict | None:
        return self.get(self.background) or self.active_model()

    def has_key(self, model_id: str) -> bool:
        if model_id not in self._key_cache:
            self._key_cache[model_id] = bool(get_key(model_id))
        return self._key_cache[model_id]

    def public(self) -> dict:
        """What the app may see. Never includes keys."""
        items = []
        for m in self.models:
            p = PROVIDERS.get(m["provider"], {})
            items.append({**m, "provider_label": p.get("label", m["provider"]), "local": bool(p.get("local")),
                          "needs_key": bool(p.get("key")), "has_key": self.has_key(m["id"]) if p.get("key") else False})
        return {"items": items, "active": self.active, "background": self.background,
                "providers": {k: {kk: v for kk, v in p.items()} for k, p in PROVIDERS.items()}}

    # ---- changes
    def save_model(self, data: dict) -> dict:
        provider = data.get("provider")
        if provider not in PROVIDERS:
            raise ValueError("Pick a provider.")
        p = PROVIDERS[provider]
        model = (data.get("model") or "").strip()
        if not model and provider != "anthropic":
            raise ValueError("Enter the model ID.")
        base = (data.get("base_url") or "").strip().rstrip("/")
        if provider.startswith("custom") and not re.match(r"^https?://", base):
            raise ValueError("Enter the provider's base URL (starting with https://).")
        ctx = int(data.get("context_tokens") or 0)
        label = (data.get("label") or "").strip() or (f"{model}" if model else p["label"])
        existing = self.get(data.get("id") or "")
        if existing:
            existing.update({"label": label, "provider": provider, "model": model, "base_url": base,
                             "context_tokens": ctx})
            m = existing
        else:
            m = self._new(label, provider, model, base, ctx)
            self.models.append(m)
        key = (data.get("api_key") or "").strip()
        if key:
            set_key(m["id"], key)
            self._key_cache[m["id"]] = True
        elif p["key"] and not self.has_key(m["id"]):
            if not existing:
                self.models.remove(m)
            raise ValueError(f"{p['label']} needs an API key.")
        if not self.active:                      # first model ever: use it everywhere
            self.active = self.background = m["id"]
        self.save()
        return m

    def delete(self, model_id: str) -> None:
        if len(self.models) <= 1:
            raise ValueError("Keep at least one model.")
        self.models = [m for m in self.models if m["id"] != model_id]
        delete_key(model_id)
        self._key_cache.pop(model_id, None)
        if self.active == model_id:
            self.active = self.models[0]["id"]
        if self.background == model_id:
            self.background = self.active
        self.save()

    def set_active(self, model_id: str) -> dict:
        if not self.get(model_id):
            raise ValueError("Unknown model.")
        self.active = model_id
        self.save()
        return self.active_model()

    def set_background(self, model_id: str) -> None:
        if not self.get(model_id):
            raise ValueError("Unknown model.")
        self.background = model_id
        self.save()


def provider_of(m: dict) -> dict:
    return PROVIDERS.get(m["provider"], PROVIDERS["custom-anthropic"])


def base_url(m: dict) -> str:
    if m.get("provider") == "ollama":
        return (m.get("base_url") or config.OLLAMA_URL).rstrip("/")
    return (m.get("base_url") or provider_of(m)["base"] or "").rstrip("/")


def is_local(m: dict) -> bool:
    return bool(provider_of(m).get("local"))


def context_window(m: dict) -> int:
    return int(m.get("context_tokens") or provider_of(m).get("ctx") or 0)
