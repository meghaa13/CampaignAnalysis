

import os
import re
import json
import requests
import pandas as pd
from urllib.parse import urlparse
from .config import model
from .utils_text import parse_and_repair
from .utils_web import fetch_page_text


# ─────────────────────────────────────────────────────────────────────────────
# STEP 1 — Discover competitor domains from live Google Ads SERPs
# ─────────────────────────────────────────────────────────────────────────────

def _discover_competitor_domains_via_serp(top_keywords: list, country: str = "in") -> list:
    """
    Searches Google (via Serper/SerpAPI/ValueSERP) for the client's top keywords
    and extracts every competitor domain that is running paid ads.
    Returns a deduped list of domains e.g. ['magicbricks.com', 'housing.com']
    """
    serper_key    = os.environ.get("SERPER_API_KEY")
    serpapi_key   = os.environ.get("SERPAPI_KEY")
    valueserp_key = os.environ.get("VALUESERP_KEY")

    domains = []

    if not any([serper_key, serpapi_key, valueserp_key]):
        print("[COMPETITOR] No SERP key — will use COMPETITOR_DOMAINS env var or property fallback list.")
        return []

    for kw in top_keywords[:6]:
        try:
            if serper_key:
                resp = requests.post(
                    "https://google.serper.dev/search",
                    headers={"X-API-KEY": serper_key, "Content-Type": "application/json"},
                    json={"q": kw, "gl": country, "hl": "en", "num": 10},
                    timeout=12,
                )
                ads = resp.json().get("ads", [])

            elif valueserp_key:
                resp = requests.get(
                    "https://api.valueserp.com/search",
                    params={"api_key": valueserp_key, "q": kw, "gl": country, "num": 10},
                    timeout=12,
                )
                ads = resp.json().get("ads", [])

            else:
                resp = requests.get(
                    "https://serpapi.com/search",
                    params={"api_key": serpapi_key, "q": kw, "gl": country, "engine": "google"},
                    timeout=12,
                )
                ads = resp.json().get("ads", [])

            for ad in ads:
                raw = ad.get("displayLink") or ad.get("displayed_link") or ad.get("link") or ""
                domain = _clean_domain(raw)
                if domain and domain not in domains:
                    domains.append(domain)

            print(f"[COMPETITOR] SERP '{kw}' → {len(ads)} ads found")
        except Exception as e:
            print(f"[COMPETITOR] SERP failed for '{kw}': {e}")

    return list(dict.fromkeys(domains))   # preserve insertion order, dedup


def _clean_domain(raw_url: str) -> str:
    """Extract bare domain (no www, no path) from a display URL or full URL."""
    if not raw_url:
        return ""
    raw_url = raw_url.strip().lower()
    if not raw_url.startswith("http"):
        raw_url = "https://" + raw_url
    try:
        domain = urlparse(raw_url).netloc.replace("www.", "").split("/")[0]
        return domain if "." in domain else ""
    except Exception:
        return ""


# ─────────────────────────────────────────────────────────────────────────────
# STEP 2 — Scrape each competitor's landing page
# ─────────────────────────────────────────────────────────────────────────────

def _scrape_competitor_lp(domain: str, char_limit: int = 2500) -> dict:
    """
    Fetches a competitor's homepage and extracts:
    - Full page text (for Gemini)
    - Hero headline (first h1 / h2 / h3)
    - CTA button/link text
    """
    url = f"https://{domain}" if not domain.startswith("http") else domain
    result = {"domain": domain, "url": url, "text": "", "headline": "", "cta": "", "error": ""}

    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        }
        resp = requests.get(url, headers=headers, timeout=12, allow_redirects=True)
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(resp.text, "html.parser")

        # Extract hero headline
        for tag in ["h1", "h2", "h3"]:
            el = soup.find(tag)
            if el:
                result["headline"] = el.get_text(strip=True)[:200]
                break

        # Extract CTA button / link text
        cta_texts = []
        for btn in soup.find_all(["button", "a"], limit=40):
            txt = btn.get_text(strip=True)
            if 2 < len(txt) < 60 and any(w in txt.lower() for w in [
                "enquire", "contact", "book", "get", "find", "search",
                "explore", "view", "register", "schedule", "visit", "call",
                "download", "know more", "learn more", "apply", "buy", "rent",
                "free", "tour", "check", "see",
            ]):
                cta_texts.append(txt)
        result["cta"] = " | ".join(list(dict.fromkeys(cta_texts))[:6])

        # Full clean text
        for tag in soup(["script", "style", "noscript", "nav", "footer", "iframe"]):
            tag.decompose()
        full_text = soup.get_text(separator=" ", strip=True)
        result["text"] = full_text[:char_limit]

        print(f"[COMPETITOR] Scraped {domain}: headline='{result['headline'][:70]}'")

    except Exception as e:
        result["error"] = str(e)
        print(f"[COMPETITOR] LP scrape failed for {domain}: {e}")

    return result


# ─────────────────────────────────────────────────────────────────────────────
# STEP 3 — Gemini competitive intelligence synthesis
# ─────────────────────────────────────────────────────────────────────────────

def _gemini_analyse_property_competitors(
    competitor_lp_data: list,
    our_lp_text: str,
    our_campaigns_summary: str,
    our_keywords_summary: str,
) -> list:
    """
    Sends all scraped competitor LP data to Gemini.
    Returns a structured JSON array — one object per competitor
    + a final STRATEGIC SUMMARY object.
    """
    if not competitor_lp_data:
        print("[COMPETITOR] No competitor LP data to analyse.")
        return []

    # Build competitor profiles block for the prompt
    comp_block = ""
    for c in competitor_lp_data:
        domain       = c.get("domain", "Unknown")
        headline     = c.get("headline", "(no headline found)")
        cta          = c.get("cta", "(no CTA found)")
        text_snippet = (c.get("text", "") or "")[:1200]
        error        = c.get("error", "")

        comp_block += f"""
━━━ COMPETITOR: {domain} ━━━
Hero Headline : {headline}
CTA Buttons   : {cta}
LP Content Snippet:
{text_snippet}
{f"[NOTE: Scrape error — {error}]" if error else ""}
"""

    prompt = f"""
You are a senior Google Ads strategist specialising in the PROPERTY / REAL ESTATE sector.
You are conducting a full competitive intelligence audit for a property advertiser.

OUR CLIENT'S LANDING PAGE:
{our_lp_text[:1000] or "(not available)"}

OUR CLIENT'S CAMPAIGN PERFORMANCE (top-line):
{our_campaigns_summary or "(not available)"}

OUR CLIENT'S TOP KEYWORDS:
{our_keywords_summary or "(not available)"}

=== COMPETITOR LANDING PAGES (scraped live) ===
{comp_block}

Your task: Return a JSON ARRAY where each object covers one competitor,
and the final object is a STRATEGIC SUMMARY.

Each COMPETITOR object MUST contain these exact keys:
- "Competitor"        : competitor domain (e.g. "magicbricks.com")
- "Threat Level"      : "High" / "Medium" / "Low"  (based on LP quality + messaging strength)
- "Data Source"       : "Landing Page + SERP"
- "LP Headline"       : their exact hero headline from the page
- "Value Proposition" : what they are promising buyers/renters (1-2 sentences)
- "CTA Strategy"      : the primary action they push users to take
- "Strengths"         : 2-3 specific things they do BETTER than our client
- "Weaknesses"        : 1-2 specific gaps or weak points on their LP
- "Insight"           : the single most important competitive takeaway
- "Recommendation"    : ONE specific action our client should take in ads/LP
                        to counter this competitor — be precise
                        (e.g. "Add 'Zero brokerage' badge since housing.com charges brokerage")

The LAST object must be the STRATEGIC SUMMARY with these exact keys:
- "Competitor"        : "STRATEGIC SUMMARY"
- "Threat Level"      : "Summary"
- "Data Source"       : "Cross-Competitor Analysis"
- "LP Headline"       : "(overall market landscape)"
- "Value Proposition" : Who is dominating and why
- "CTA Strategy"      : Most common CTA pattern across all competitors
- "Strengths"         : What collectively makes these competitors strong
- "Weaknesses"        : Common gaps across ALL competitors our client can exploit
- "Insight"           : The #1 messaging gap our client should fill immediately
- "Recommendation"    : Top 3 priority actions ranked by impact (numbered list)

STRICT RULES:
- Return a valid JSON array ONLY. No markdown fences. No text outside the array.
- Quote actual headlines, CTAs, and phrases scraped from the competitor LPs.
- Do NOT invent metrics or data not present in the content above.
- If a competitor LP failed to load, mark Threat Level as "Unknown" and note low confidence.
- Minimum: 3 competitor objects + 1 strategic summary.
- Maximum: 7 competitor objects + 1 strategic summary.
"""

    try:
        raw = (model.generate_content(prompt).text or "").strip()
        insights = parse_and_repair(raw)
        if isinstance(insights, list) and len(insights) > 0:
            return insights
        print(f"[COMPETITOR] Gemini returned invalid JSON. Raw preview: {raw[:300]}")
        return []
    except Exception as e:
        print(f"[ERROR] Gemini property competitor analysis failed: {e}")
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Helper — build readable summaries for the Gemini prompt
# ─────────────────────────────────────────────────────────────────────────────

def _summarise_campaigns(df: pd.DataFrame) -> str:
    if df is None or df.empty:
        return ""
    cols = [c for c in ["Campaign Name", "Cost ($)", "Clicks", "Conversions", "CTR", "CPA ($)"] if c in df.columns]
    if not cols:
        return ""
    return df[cols].head(10).to_string(index=False)


def _summarise_keywords(df: pd.DataFrame) -> str:
    if df is None or df.empty:
        return ""
    cols = [c for c in ["Keyword", "Cost ($)", "Clicks", "Conversions", "Quality Score"] if c in df.columns]
    if not cols or "Cost ($)" not in df.columns:
        return ""
    return df.sort_values("Cost ($)", ascending=False)[cols].head(15).to_string(index=False)


# ─────────────────────────────────────────────────────────────────────────────
# Default competitor fallback  (used ONLY when no SERP key + no env var set)
# ─────────────────────────────────────────────────────────────────────────────

def _infer_fallback_domains(top_keywords: list, site_url: str) -> list:
    """
    Infer industry-relevant fallback competitor domains from client keywords / site URL.
    Avoids hardcoding any sector — works for ANY client.
    Priority:
      1. COMPETITOR_DOMAINS env var (comma-separated) — set this in Render dashboard
      2. Derive from top keywords using broad industry signals
      3. Absolute last resort: 3 generic digital marketing competitors
    """
    # ── Priority 1: explicit override ─────────────────────────────────────
    env_override = os.environ.get("COMPETITOR_DOMAINS", "").strip()
    if env_override:
        domains = [d.strip() for d in env_override.split(",") if d.strip()]
        if domains:
            print(f"[COMPETITOR] Using COMPETITOR_DOMAINS env var: {domains}")
            return domains

    # ── Priority 2: keyword-signal based guessing ─────────────────────────
    kw_blob = " ".join(top_keywords).lower() if top_keywords else ""
    site_low = (site_url or "").lower()

    # Map keyword signals → typical competitor domains for that niche
    NICHE_SIGNALS = [
        (["video brochure", "video card", "lcd video", "video mailer", "video box"],
         ["liquidimaging.com", "igotopromo.com", "broadcastprintmedia.com",
          "videobrochures.com", "videoplus.com"]),
        (["real estate", "property", "flat", "apartment", "villa", "buy home"],
         ["zillow.com", "realtor.com", "trulia.com", "redfin.com", "homes.com"]),
        (["hotel", "resort", "hospitality", "booking", "stay"],
         ["booking.com", "hotels.com", "expedia.com", "agoda.com", "airbnb.com"]),
        (["software", "saas", "crm", "erp", "cloud platform"],
         ["salesforce.com", "hubspot.com", "zoho.com", "freshworks.com", "pipedrive.com"]),
        (["ecommerce", "online store", "shopify", "woocommerce", "product"],
         ["shopify.com", "bigcommerce.com", "wix.com", "squarespace.com", "weebly.com"]),
        (["education", "course", "online learning", "certification", "training"],
         ["udemy.com", "coursera.org", "edx.org", "skillshare.com", "pluralsight.com"]),
        (["insurance", "policy", "premium", "coverage", "health plan"],
         ["policybazaar.com", "coverfox.com", "acko.com", "digit.in", "turtlemint.com"]),
        (["finance", "loan", "credit", "invest", "mutual fund"],
         ["groww.in", "zerodha.com", "angelone.in", "paytmmoney.com", "icicidirect.com"]),
    ]

    for signals, fallbacks in NICHE_SIGNALS:
        if any(sig in kw_blob or sig in site_low for sig in signals):
            print(f"[COMPETITOR] Keyword-matched niche → using: {fallbacks[:3]}")
            return fallbacks

    # ── Priority 3: truly generic last resort ─────────────────────────────
    print("[COMPETITOR] No niche match — using generic digital competitors as placeholder.")
    print("[COMPETITOR] ⚠️  Set COMPETITOR_DOMAINS env var in Render dashboard for accurate results!")
    return ["semrush.com", "ahrefs.com", "similarweb.com"]


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point  (same signature as before — fully backward compatible)
# ─────────────────────────────────────────────────────────────────────────────

def generate_competitor_insights(
    google_ads_client,
    kw_df,
    lp_df,
    site_url,
    genai_model,        # kept for API compatibility — not used internally
    customer_id,
    date_range="LAST_30_DAYS",
):
    """
    Property competitor intelligence pipeline:

      1. Derive top keywords from the client's keyword spend data
      2. Query Google SERP to discover which property companies are bidding
         on those same keywords (via Serper / SerpAPI / ValueSERP)
      3. Scrape each competitor's homepage — headline, CTA, full text
      4. Fetch our client's best-converting landing page for comparison
      5. Gemini synthesises a full competitive analysis per competitor
      6. Returns a DataFrame (one row = one competitor + a summary row)

    Fallback: if no SERP key is set, uses _PROPERTY_FALLBACK_DOMAINS and
    still scrapes + analyses them via Gemini.
    """
    print("[COMPETITOR] Starting property competitor intelligence pipeline...")

    # ── Step 1: Derive top keywords ──────────────────────────────────────────
    top_keywords = []
    if kw_df is not None and not kw_df.empty and "Keyword" in kw_df.columns:
        if "Cost ($)" in kw_df.columns:
            top_keywords = (
                kw_df.sort_values("Cost ($)", ascending=False)["Keyword"]
                .dropna().unique().tolist()[:8]
            )
        else:
            top_keywords = kw_df["Keyword"].dropna().unique().tolist()[:8]

    if not top_keywords:
        top_keywords = ["property for sale", "apartments for sale", "buy flat", "real estate"]

    print(f"[COMPETITOR] Searching SERP with {len(top_keywords)} keywords: {top_keywords[:3]}...")

    # ── Step 2: Discover competitor domains via SERP ─────────────────────────
    competitor_domains = _discover_competitor_domains_via_serp(top_keywords)

    # Add any manually configured domains
    env_domains = os.environ.get("COMPETITOR_DOMAINS", "")
    if env_domains:
        for d in env_domains.split(","):
            d = d.strip()
            if d and d not in competitor_domains:
                competitor_domains.append(d)

    # Fall back to smart keyword-derived defaults if nothing was found
    if not competitor_domains:
        print("[COMPETITOR] No SERP data — deriving competitors from keywords/site.")
        competitor_domains = _infer_fallback_domains(top_keywords, site_url)

    # Remove our own domain
    our_domain = _clean_domain(site_url) if site_url else ""
    if our_domain:
        competitor_domains = [d for d in competitor_domains if our_domain not in d]

    # Cap at 6 to keep runtime reasonable (~30–60 s)
    competitor_domains = list(dict.fromkeys(competitor_domains))[:6]
    print(f"[COMPETITOR] Analysing {len(competitor_domains)} competitors: {competitor_domains}")

    # ── Step 3: Scrape each competitor's LP ──────────────────────────────────
    competitor_lp_data = [_scrape_competitor_lp(d) for d in competitor_domains]

    # ── Step 4: Our own best-converting LP ───────────────────────────────────
    our_lp_text = ""
    try:
        best_url = site_url
        if (lp_df is not None and not lp_df.empty
                and "Final URL" in lp_df.columns
                and "Conversions" in lp_df.columns):
            candidate = (
                lp_df.sort_values("Conversions", ascending=False)["Final URL"]
                .dropna().iloc[0]
            )
            best_url = candidate or site_url
        if best_url:
            our_lp_text = (fetch_page_text(best_url) or "")[:2500]
    except Exception as e:
        print(f"[COMPETITOR] Our LP fetch failed: {e}")

    # ── Step 5: Gemini synthesis ──────────────────────────────────────────────
    insights = _gemini_analyse_property_competitors(
        competitor_lp_data,
        our_lp_text,
        _summarise_campaigns(kw_df),
        _summarise_keywords(kw_df),
    )

    # ── Step 6: Build output DataFrame ───────────────────────────────────────
    expected_cols = [
        "Competitor", "Threat Level", "Data Source", "LP Headline",
        "Value Proposition", "CTA Strategy", "Strengths", "Weaknesses",
        "Insight", "Recommendation",
    ]

    if not insights:
        print("[COMPETITOR] No Gemini insights. Returning raw LP scrape data as fallback.")
        rows = []
        for c in competitor_lp_data:
            rows.append({
                "Competitor":        c.get("domain", ""),
                "Threat Level":      "Unknown",
                "Data Source":       "Landing Page Scrape",
                "LP Headline":       c.get("headline", ""),
                "Value Proposition": "",
                "CTA Strategy":      c.get("cta", ""),
                "Strengths":         "",
                "Weaknesses":        "",
                "Insight":           f"LP scraped from {c.get('url', '')}. Gemini analysis unavailable.",
                "Recommendation":    "Review competitor LP manually and compare messaging with client.",
            })
        return pd.DataFrame(rows) if rows else pd.DataFrame(columns=expected_cols)

    result_df = pd.DataFrame(insights)
    for col in expected_cols:
        if col not in result_df.columns:
            result_df[col] = ""

    print(f"[COMPETITOR] Done: {len(result_df)} rows generated ({len(result_df)-1} competitors + summary).")
    return result_df