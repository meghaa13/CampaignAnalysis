# config.py (FIXED: Groq model + geo CSV loading)
import os
from pathlib import Path
import sys
import shutil
import yaml

# optional heavy imports:
try:
    import pandas as pd
except Exception:
    pd = None

try:
    from google import genai as _genai_sdk
except Exception:
    _genai_sdk = None

import requests as _requests

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
        return response

# ── Provider 2: Groq (free tier) ─────────────────────────────────────────────
class _GroqModel:
    """Groq via pure REST — no groq package needed. Free 14,400 req/day."""
    def __init__(self, api_key, model_name):
        self._api_key = api_key
        self._model_name = model_name

    # Groq context window: 128k tokens ≈ ~96k chars safe limit
    _MAX_PROMPT_CHARS = 80_000
    _RETRY_WAITS = (3, 6, 12)   # seconds to wait between rate-limit retries

    def generate_content(self, prompt, **kwargs):
        import time as _time
        # Truncate oversized prompts — 400 errors often caused by token overflow
        if isinstance(prompt, str) and len(prompt) > self._MAX_PROMPT_CHARS:
            prompt = prompt[:self._MAX_PROMPT_CHARS] + "\n\n[PROMPT TRUNCATED FOR LENGTH]"

        last_err = None
        for wait in (0, *self._RETRY_WAITS):  # first attempt is immediate
            if wait:
                print(f"[LLM] Groq rate-limit — waiting {wait}s before retry...")
                _time.sleep(wait)
            resp = _requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
                json={"model": self._model_name, "messages": [{"role": "user", "content": prompt}],
                      "max_tokens": 2048, "temperature": 0.3},
                timeout=60,
            )
            if resp.status_code == 429:
                last_err = f"rate_limit: {resp.text[:200]}"
                continue   # retry after wait
            if resp.status_code == 400:
                raise RuntimeError(f"groq_bad_request: {resp.text[:400]}")
            resp.raise_for_status()
            return DummyResponse(resp.json()["choices"][0]["message"]["content"] or "")

        raise RuntimeError(last_err or "Groq rate-limit exceeded after retries")

# ── Provider 3: OpenRouter (free models) ─────────────────────────────────────
class _OpenRouterModel:
    """
    OpenRouter gives access to free models (mistral-7b, llama-3 etc).
    Free tier: generous daily limits. Sign up at https://openrouter.ai
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
        self._providers = providers
        self._current = 0

    def _is_quota_error(self, exc: Exception) -> bool:
        msg = str(exc).lower()
        return any(sig.lower() in msg for sig in self.QUOTA_SIGNALS)

    def generate_content(self, prompt, **kwargs):
        # Always attempt from the beginning of the chain — never permanently skip a provider
        for i in range(len(self._providers)):
            name, provider = self._providers[i]
            try:
                result = provider.generate_content(prompt, **kwargs)
                if i != self._current:
                    print(f"[LLM] Active provider: {name}")
                    self._current = i
                return result
            except Exception as e:
                err_msg = str(e)
                if self._is_quota_error(e):
                    print(f"[LLM] {name} quota/rate-limit hit: {err_msg[:200]}. Trying next provider...")
                else:
                    print(f"[LLM] {name} error: {err_msg[:300]}. Trying next provider...")
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

# Provider 2 — Groq via REST ✅ FIXED MODEL NAME
if GROQ_API_KEY:
    try:
        _groq_model = os.environ.get("GROQ_MODEL", "llama-3.1-8b-instant")   # 15,000+ TPM vs 70b's 1,200 TPM
        _providers.append(("Groq", _GroqModel(GROQ_API_KEY, _groq_model)))
        print(f"[LLM] ✅ Groq ready ({_groq_model})")
    except Exception as e:
        print(f"[LLM] Groq init failed: {e}")

# Provider 3 — OpenRouter (free models)
if OPENROUTER_API_KEY:
    try:
        _or_model = os.environ.get("OPENROUTER_MODEL", "google/gemini-2.0-flash-exp:free")
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
if any(base_config.values()):
    try:
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
                f"Underlying error: {repr(self._load_exc)}"
            )
        return getattr(self._client, name)

client = LazyGoogleAdsClient(config_path="base_google-ads.yaml")
customer_id = None

# === Chrome Config (non-fatal) ===
def get_chrome_exec():
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

            linux_candidates = [
                "/usr/bin/chromium-browser",
                "/usr/bin/chromium",
                "/usr/bin/google-chrome",
                "/usr/bin/google-chrome-stable",
            ]
            for p in linux_candidates:
                if Path(p).exists():
                    return p

            found = shutil.which("chrome") or shutil.which("chromium") or shutil.which("google-chrome")
            if found:
                return found
    except Exception:
        pass
    return None

CHROME_PATH = os.environ.get("CHROME_PATH") or None

if getattr(sys, "frozen", False):
    USER_DATA_DIR = str(Path.home() / ".google_ads_userdata")
else:
    USER_DATA_DIR = os.path.abspath("ChromeDebugProfile")

DEBUGGING_PORT = 9222

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

# ✅ FIXED: Geo Lookup with Multiple Fallback Paths
print("[GEO] Starting geo CSV load...")
GEO_LOOKUP_DF = None

# Resolve paths relative to this config.py file AND the process CWD
_THIS_DIR   = Path(__file__).resolve().parent          # audit/
_APP_DIR    = _THIS_DIR.parent                         # project root

_GEO_PATHS = [
    os.environ.get("GEO_CSV_PATH"),
    os.environ.get("GEOTARGETS_PATH"),
    str(_APP_DIR / "geotargets.csv"),          # ← project-root relative (most reliable)
    str(_THIS_DIR / "geotargets.csv"),         # ← next to config.py
    "/tmp/geotargets.csv",
    "/tmp/geotargets-2025-07-15.csv",
    "geotargets-2025-07-15.csv",
    "geotargets.csv",
]

for path in _GEO_PATHS:
    if not path or not os.path.exists(path):
        continue
    try:
        if pd is None:
            break
        
        GEO_LOOKUP_DF = pd.read_csv(path, dtype=str)
        
        # ✅ FIXED: Flexible column detection
        id_col = None
        for col in GEO_LOOKUP_DF.columns:
            col_lower = col.lower()
            if "criteria" in col_lower or "criterion" in col_lower:
                id_col = col
                break
        
        if not id_col:
            print(f"[GEO] CSV at {path} missing ID column, skipping...")
            GEO_LOOKUP_DF = None
            continue
        
        # Filter active + set index
        if "Status" in GEO_LOOKUP_DF.columns:
            GEO_LOOKUP_DF = GEO_LOOKUP_DF[GEO_LOOKUP_DF["Status"] == "Active"]
        
        GEO_LOOKUP_DF[id_col] = pd.to_numeric(GEO_LOOKUP_DF[id_col], errors='coerce')
        GEO_LOOKUP_DF = GEO_LOOKUP_DF.dropna(subset=[id_col])
        GEO_LOOKUP_DF[id_col] = GEO_LOOKUP_DF[id_col].astype(int)
        GEO_LOOKUP_DF.set_index(id_col, inplace=True)
        
        print(f"[GEO] ✅ Loaded {len(GEO_LOOKUP_DF)} geotargets from {path}")
        break
        
    except Exception as e:
        print(f"[GEO] Failed to load {path}: {e}")
        GEO_LOOKUP_DF = None
        continue

if GEO_LOOKUP_DF is None:
    print("[GEO] No geo CSV found — will show GeoID fallbacks")
    GEO_LOOKUP_DF = pd.DataFrame() if pd else None

# === Ensure folders exist ===
REPORT_IMAGES_DIR = os.environ.get("REPORT_IMAGES_DIR", "/tmp/report_images")
GENERATED_REPORTS_DIR = os.environ.get("UPLOAD_FOLDER", "/tmp/generated_reports")
USER_TOKENS_DIR = os.environ.get("USER_TOKENS_DIR", "/tmp/user_tokens")
os.makedirs(REPORT_IMAGES_DIR, exist_ok=True)
os.makedirs(GENERATED_REPORTS_DIR, exist_ok=True)
os.makedirs(USER_TOKENS_DIR, exist_ok=True)