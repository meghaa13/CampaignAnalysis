# config.py (patched for Cloud Run readiness)
import os
from pathlib import Path
import sys
import traceback
import shutil
import yaml

# optional heavy imports:
try:
    print("DEBUG: config.py: importing pandas...")
    import pandas as pd
    print("DEBUG: config.py: pandas imported.")
except Exception:
    pd = None

# google-ads imports are done on demand inside LazyGoogleAdsClient

# ── Generative AI — Multi-LLM fallback chain ─────────────────────────────────
# Priority: Gemini (google.genai) → Groq (llama3) → OpenRouter (free models)
# Every provider wraps to the same .generate_content(prompt) interface.
# Config via env vars:
#   GEMINI_API_KEY      → primary (google.genai SDK)
#   GROQ_API_KEY        → fallback 1 (free tier, very fast)
#   OPENROUTER_API_KEY  → fallback 2 (free tier, many models)
# If none set → DummyModel (rule-based output, no AI)
# ─────────────────────────────────────────────────────────────────────────────

try:
    from google import genai as _genai_sdk
except Exception:
    _genai_sdk = None

import requests as _requests  # Groq uses pure REST — no groq package needed

class DummyResponse:
    def __init__(self, text=""):
        self.text = text

class DummyModel:
    def generate_content(self, *args, **kwargs):
        return DummyResponse(
            '[{"Characteristic":"AI Disabled","Insight":"No LLM API key configured",'
            '"Recommendation":"Set GEMINI_API_KEY or GROQ_API_KEY or OPENROUTER_API_KEY"}]'
        )


# ── Provider 1: Gemini ────────────────────────────────────────────────────────
class _GeminiModel:
    """Wraps google.genai SDK. Raises on quota/auth errors so chain can fallback."""
    def __init__(self, client, model_name):
        self._client = client
        self._model_name = model_name

    def generate_content(self, prompt, **kwargs):
        response = self._client.models.generate_content(
            model=self._model_name,
            contents=prompt,
        )
        return response   # response.text is the string


# ── Provider 2: Groq (free tier) ─────────────────────────────────────────────
class _GroqModel:
    """Groq via pure REST — no groq package needed. Free 14,400 req/day."""
    def __init__(self, api_key, model_name):
        self._api_key = api_key
        self._model_name = model_name

    def generate_content(self, prompt, **kwargs):
        resp = _requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
            json={"model": self._model_name, "messages": [{"role": "user", "content": prompt}],
                  "max_tokens": 4096, "temperature": 0.3},
            timeout=60,
        )
        if resp.status_code == 429:
            raise RuntimeError(f"rate_limit: {resp.text[:100]}")
        resp.raise_for_status()
        return DummyResponse(resp.json()["choices"][0]["message"]["content"] or "")


# ── Provider 3: OpenRouter (free models) ─────────────────────────────────────
class _OpenRouterModel:
    """
    OpenRouter gives access to free models (mistral-7b, llama-3 etc).
    Free tier: generous daily limits. Sign up at https://openrouter.ai
    Set OPENROUTER_MODEL env var to change model (default: mistralai/mistral-7b-instruct:free)
    """
    def __init__(self, api_key, model_name):
        self._api_key = api_key
        self._model_name = model_name

    def generate_content(self, prompt, **kwargs):
        resp = _requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self._model_name,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 4096,
                "temperature": 0.3,
            },
            timeout=60,
        )
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"] or ""
        return DummyResponse(text)


# ── Fallback chain wrapper ────────────────────────────────────────────────────
class _FallbackModel:
    """
    Tries each provider in order. On quota/auth/rate-limit error, moves to next.
    Logs which provider is active. Never raises — worst case returns DummyModel output.
    """
    QUOTA_SIGNALS = [
        "RESOURCE_EXHAUSTED", "quota", "rate_limit", "rate limit",
        "429", "insufficient_quota", "exceeded", "too many requests",
    ]

    def __init__(self, providers: list):
        # providers: list of (name, model_obj)
        self._providers = providers
        self._current = 0

    def _is_quota_error(self, exc: Exception) -> bool:
        msg = str(exc).lower()
        return any(sig.lower() in msg for sig in self.QUOTA_SIGNALS)

    def generate_content(self, prompt, **kwargs):
        for i in range(self._current, len(self._providers)):
            name, provider = self._providers[i]
            try:
                result = provider.generate_content(prompt, **kwargs)
                if i != self._current:
                    print(f"[LLM] Switched to provider: {name}")
                    self._current = i
                return result
            except Exception as e:
                if self._is_quota_error(e):
                    print(f"[LLM] {name} quota/rate-limit hit: {e}. Trying next provider...")
                    continue
                else:
                    # Non-quota error (bad prompt, parse error etc) — still try next
                    print(f"[LLM] {name} error: {e}. Trying next provider...")
                    continue

        print("[LLM] All providers exhausted. Returning dummy response.")
        return DummyModel().generate_content(prompt)


# ── Build the model ───────────────────────────────────────────────────────────
GEMINI_API_KEY      = os.environ.get("GEMINI_API_KEY")
GROQ_API_KEY        = os.environ.get("GROQ_API_KEY")
OPENROUTER_API_KEY  = os.environ.get("OPENROUTER_API_KEY")

_providers = []

# Provider 1 — Gemini
if GEMINI_API_KEY and _genai_sdk is not None:
    try:
        _gc = _genai_sdk.Client(api_key=GEMINI_API_KEY)
        _gm_name = os.environ.get("GEMINI_MODEL", "gemini-1.5-flash")
        _providers.append(("Gemini", _GeminiModel(_gc, _gm_name)))
        print(f"[LLM] Gemini ready ({_gm_name})")
    except Exception as e:
        print(f"[LLM] Gemini init failed: {e}")

# Provider 2 — Groq via REST (just needs GROQ_API_KEY, no package)
if GROQ_API_KEY:
    try:
        _groq_model = os.environ.get("GROQ_MODEL", "llama3-8b-8192")
        _providers.append(("Groq", _GroqModel(GROQ_API_KEY, _groq_model)))
        print(f"[LLM] ✅ Groq ready ({_groq_model})")
    except Exception as e:
        print(f"[LLM] Groq init failed: {e}")

# Provider 3 — OpenRouter (free models)
if OPENROUTER_API_KEY:
    try:
        _or_model = os.environ.get("OPENROUTER_MODEL", "mistralai/mistral-7b-instruct:free")
        _providers.append(("OpenRouter", _OpenRouterModel(OPENROUTER_API_KEY, _or_model)))
        print(f"[LLM] OpenRouter ready ({_or_model})")
    except Exception as e:
        print(f"[LLM] OpenRouter init failed: {e}")

if _providers:
    model = _FallbackModel(_providers)
    print(f"[LLM] Active chain: {' → '.join(n for n, _ in _providers)}")
else:
    model = DummyModel()
    print("[LLM] No LLM API keys set. Set GEMINI_API_KEY, GROQ_API_KEY, or OPENROUTER_API_KEY.")

# --- Write base_google-ads.yaml dynamically from env vars ---
base_yaml_path = "base_google-ads.yaml"
base_config = {
    "developer_token": os.environ.get("GOOGLE_ADS_DEVELOPER_TOKEN"),
    "client_id": os.environ.get("GOOGLE_ADS_CLIENT_ID"),
    "client_secret": os.environ.get("GOOGLE_ADS_CLIENT_SECRET"),
    "refresh_token": os.environ.get("GOOGLE_ADS_REFRESH_TOKEN"),
    "use_proto_plus": True
}
if any(base_config.values()):  # write only if something is provided
    try:
        # Avoid unnecessary writes if content hasn't changed
        existing_data = {}
        if os.path.exists(base_yaml_path):
            with open(base_yaml_path, "r", encoding="utf-8") as f:
                existing_data = yaml.safe_load(f) or {}
        
        if existing_data != base_config:
            with open(base_yaml_path, "w", encoding="utf-8") as f:
                yaml.safe_dump(base_config, f)
            print(f"[INFO] Updated {base_yaml_path}")
    except Exception as e:
        print(f"[WARN] Could not write {base_yaml_path}: {e}")

# === Google Ads Client loader ===
def load_google_ads_client(config_path="base_google-ads.yaml"):
    from google.ads.googleads.client import GoogleAdsClient
    return GoogleAdsClient.load_from_storage(config_path)

def safe_load_google_ads_client(config_path="base_google-ads.yaml"):
    try:
        from google.ads.googleads.client import GoogleAdsClient
        return GoogleAdsClient.load_from_storage(config_path)
    except Exception as e:
        print(f"[WARN] safe_load_google_ads_client: failed to load '{config_path}': {e}")
        return None

class LazyGoogleAdsClient:
    def __init__(self, config_path="base_google-ads.yaml"):
        self._config_path = config_path
        self._client = None
        self._load_exc = None

    def _ensure_loaded(self):
        if self._client is not None:
            return
        try:
            from google.ads.googleads.client import GoogleAdsClient
            self._client = GoogleAdsClient.load_from_storage(self._config_path)
        except Exception as e:
            self._load_exc = e
            self._client = None

    def __getattr__(self, name):
        self._ensure_loaded()
        if self._client is None:
            raise RuntimeError(
                f"Google Ads client is not available. Failed to load '{self._config_path}'. "
                "This usually means your OAuth2 credentials (refresh_token) are missing or invalid. "
                "Use the app's auth flow to create per-user YAMLs in user_tokens/, or call "
                "safe_load_google_ads_client(auth_path) / load_google_ads_client(auth_path) manually. "
                f"Underlying error: {repr(self._load_exc)}"
            )
        return getattr(self._client, name)

client = LazyGoogleAdsClient(config_path="base_google-ads.yaml")

customer_id = None

# === Chrome Debugging Config (non-fatal on import) ===
def get_chrome_exec():
    # Try environment override first
    env_path = os.environ.get("CHROME_PATH")
    if env_path:
        return env_path
    try:
        if getattr(sys, "frozen", False):
            exe_dir = Path(sys.executable).parent
            chrome_path = exe_dir / "bundled_chromium" / "chrome.exe"
            if chrome_path.exists():
                return str(chrome_path.resolve())
        else:
            base_dir = Path(__file__).resolve().parent
            bundled_path = base_dir / "bundled_chromium" / "chrome.exe"
            if bundled_path.exists():
                return str(bundled_path.resolve())

            # Linux/Cloud Run: try typical chromium paths
            linux_candidates = [
                "/usr/bin/chromium-browser",
                "/usr/bin/chromium",
                "/usr/bin/google-chrome",
                "/usr/bin/google-chrome-stable",
            ]
            for p in linux_candidates:
                if Path(p).exists():
                    return p

            # try PATH
            found = shutil.which("chrome") or shutil.which("chromium") or shutil.which("google-chrome")
            if found:
                return found
    except Exception:
        pass
    # Not fatal on import — only raise if caller needs it.
    return None

# DO NOT call get_chrome_exec() at import if you don't need Chrome. Let callers decide.

CHROME_PATH = os.environ.get("CHROME_PATH") or None

# Local dev: store profile inside project dir
# Frozen exe: store profile in user home
if getattr(sys, "frozen", False):
    USER_DATA_DIR = str(Path.home() / ".google_ads_userdata")
else:
    USER_DATA_DIR = os.path.abspath("ChromeDebugProfile")

DEBUGGING_PORT = 9222  # keep as int (not string)

# === Environment Constants ===
LANGUAGE = "English"
DEVICE = "Desktop"

# === Mapping Dictionaries ===
MATCH_TYPE_MAP = {0: "UNSPECIFIED", 1: "UNKNOWN", 2: "EXACT", 3: "PHRASE", 4: "BROAD"}
STATUS_MAP = {0: "UNKNOWN", 1: "UNKNOWN", 2: "ENABLED", 3: "PAUSED", 4: "REMOVED"}
BID_STRATEGY_MAP = {
    0: "UNSPECIFIED",1: "UNKNOWN",2: "ENHANCED_CPC",3: "MANUAL_CPC",4: "MANUAL_CPM",
    5: "PAGE_ONE_PROMOTED", 6: "TARGET_CPA",
    7: "TARGET_OUTRANK_SHARE", 8: "TARGET_ROAS", 9: "TARGET_SPEND",
    10: "MAXIMIZE_CONVERSIONS", 11: "MAXIMIZE_CONVERSION_VALUE", 12: "PERCENT_CPC",
    13: "MANUAL_CPV", 14: "TARGET_CPM", 15: "TARGET_IMPRESSION_SHARE", 16: "COMMISSION",
}

# === Geo Lookup ===
print("DEBUG: config.py: starting geo lookup init...")
GEO_LOOKUP_DF = None
GEO_CSV_PATH = os.environ.get("GEO_CSV_PATH", "geotargets-2025-07-15.csv")
print(f"DEBUG: config.py: GEO_CSV_PATH is {GEO_CSV_PATH}")
try:
    if GEO_CSV_PATH.startswith("gs://"):
        # download via storage client if available
        from google.cloud import storage
        import io
        try:
            # Short timeout to prevent hangs on Windows local dev
            storage_client = storage.Client()
            bucket_name, blob_path = GEO_CSV_PATH[5:].split("/", 1)
            blob = storage_client.bucket(bucket_name).blob(blob_path)
            data = blob.download_as_bytes(timeout=10)
            if pd is not None:
                GEO_LOOKUP_DF = pd.read_csv(io.BytesIO(data))
        except Exception as e:
            print(f"[WARN] config.py: GCS download for {GEO_CSV_PATH} failed or timed out: {e}")
            # Try to see if it exists locally as well
            local_fallback = GEO_CSV_PATH.split("/")[-1]
            if os.path.exists(local_fallback) and pd is not None:
                GEO_LOOKUP_DF = pd.read_csv(local_fallback)
    else:
        if pd is not None and os.path.exists(GEO_CSV_PATH):
            GEO_LOOKUP_DF = pd.read_csv(GEO_CSV_PATH)
except Exception as e:
    print(f"[WARN] Could not load geotarget CSV at {GEO_CSV_PATH}: {e}")
    GEO_LOOKUP_DF = None

if GEO_LOOKUP_DF is None:
    # keep a usable empty DataFrame (code that expects it should handle empty)
    if pd is not None:
        GEO_LOOKUP_DF = pd.DataFrame()
else:
    try:
        GEO_LOOKUP_DF = GEO_LOOKUP_DF[GEO_LOOKUP_DF["Status"] == "Active"]
        GEO_LOOKUP_DF.set_index("Criteria ID", inplace=True)
    except Exception as e:
        print(f"[WARN] Error post-processing GEO_LOOKUP_DF: {e}")

# === Ensure folders exist ===
REPORT_IMAGES_DIR = os.environ.get("REPORT_IMAGES_DIR", "/tmp/report_images")
GENERATED_REPORTS_DIR = os.environ.get("UPLOAD_FOLDER", "/tmp/generated_reports")
USER_TOKENS_DIR = os.environ.get("USER_TOKENS_DIR", "/tmp/user_tokens")
os.makedirs(REPORT_IMAGES_DIR, exist_ok=True)
os.makedirs(GENERATED_REPORTS_DIR, exist_ok=True)
os.makedirs(USER_TOKENS_DIR, exist_ok=True)