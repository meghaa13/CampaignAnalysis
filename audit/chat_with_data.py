# chat_with_data.py
# ─────────────────────────────────────────────────────────────────────────────
# Embedded "Chat with your Google Ads data" — lives INSIDE your existing app.
# No Claude Desktop. No separate site. Just add these routes/functions.
#
# HOW IT WORKS:
#   User asks a question in your app's chat UI
#   → This module loads cached data from session
#   → Calls Groq/Gemini via the fallback chain in config.py
#   → Returns structured answer
#
# FLASK INTEGRATION (add to your app.py):
#   from audit.chat_with_data import chat_bp
#   app.register_blueprint(chat_bp, url_prefix="/chat")
#   Then visit: yourapp.com/chat
#
# STREAMLIT INTEGRATION:
#   from audit.chat_with_data import streamlit_chat_page
#   streamlit_chat_page()
# ─────────────────────────────────────────────────────────────────────────────

import os
import re
import json
import pandas as pd
from datetime import datetime, timedelta
from .config import model

# ── Data store: Flask session (survives multi-worker Cloud Run) ──────────────
# Falls back to in-process dict if session not available (e.g. CLI use).
_DATA_CACHE = {}   # fallback in-process cache

def _session_key(name): return f"__chat_{name}"

def load_data_into_chat(campaigns_df=None, keywords_df=None, geo_df=None,
                         search_terms_df=None, hourly_df=None, lp_df=None):
    """Store serialised DataFrames so any worker can access them."""
    mapping = {
        "campaigns_df": campaigns_df, "keywords_df": keywords_df,
        "geo_df": geo_df, "search_terms_df": search_terms_df,
        "hourly_df": hourly_df, "lp_df": lp_df,
    }
    for key, df in mapping.items():
        if df is not None:
            try:
                # Try Flask session first — store up to 500 rows for daily data
                from flask import session as _fs
                _fs[_session_key(key)] = df.head(500).to_json(orient="records")
            except Exception:
                pass
            # Always keep in-process fallback (full data)
            _DATA_CACHE[key] = df
    loaded = [k for k, v in mapping.items() if v is not None]
    print(f"[CHAT] Data loaded into chat: {loaded}")


def _get_df(key):
    """Get DataFrame from session (multi-worker safe) or in-process cache."""
    # Try in-process first (has full data with Date column)
    cached = _DATA_CACHE.get(key)
    if cached is not None:
        return cached
    # Fallback to session
    try:
        from flask import session as _fs
        raw = _fs.get(_session_key(key))
        if raw:
            return pd.read_json(raw, orient="records")
    except Exception:
        pass
    return None


def _all_slot_keys():
    return ["campaigns_df", "keywords_df", "geo_df", "search_terms_df", "hourly_df", "lp_df"]


# ── Date-range parsing & filtering ──────────────────────────────────────────

def _parse_date_filter(question: str):
    """Parse date range references from user question. Returns (start_date, end_date) or None."""
    q = question.lower()
    today = datetime.now().date()

    # "last N days"
    m = re.search(r'last\s+(\d+)\s+days?', q)
    if m:
        n = int(m.group(1))
        return (today - timedelta(days=n), today)

    # "past N days"
    m = re.search(r'past\s+(\d+)\s+days?', q)
    if m:
        n = int(m.group(1))
        return (today - timedelta(days=n), today)

    # "last N weeks"
    m = re.search(r'(?:last|past)\s+(\d+)\s+weeks?', q)
    if m:
        n = int(m.group(1))
        return (today - timedelta(weeks=n), today)

    # "last week"
    if 'last week' in q:
        return (today - timedelta(days=7), today)

    # "this week"
    if 'this week' in q:
        start = today - timedelta(days=today.weekday())
        return (start, today)

    # "last month"
    if 'last month' in q:
        return (today - timedelta(days=30), today)

    # "yesterday"
    if 'yesterday' in q:
        return (today - timedelta(days=1), today - timedelta(days=1))

    # "today"
    if 'today' in q and ('data' in q or 'performance' in q or 'show' in q or 'give' in q):
        return (today, today)

    return None


def _filter_df_by_dates(df, start_date, end_date):
    """Filter a DataFrame by Date column to the given range."""
    if df is None or df.empty or "Date" not in df.columns:
        return df
    df = df.copy()
    df["_date_parsed"] = pd.to_datetime(df["Date"], errors="coerce").dt.date
    mask = (df["_date_parsed"] >= start_date) & (df["_date_parsed"] <= end_date)
    filtered = df[mask].drop(columns=["_date_parsed"])
    return filtered


def _aggregate_filtered_campaigns(df):
    """Aggregate filtered daily campaign data into per-campaign summary."""
    if df is None or df.empty:
        return df

    group_cols = [c for c in ["Campaign Name"] if c in df.columns]
    if not group_cols:
        return df

    sum_cols = {c: "sum" for c in ["Impressions", "Clicks", "Cost ($)", "Conversions"]
                if c in df.columns}
    if not sum_cols:
        return df

    agg = df.groupby(group_cols, as_index=False).agg(sum_cols)

    if "Clicks" in agg.columns and "Impressions" in agg.columns:
        agg["CTR"] = (agg["Clicks"] / agg["Impressions"].replace(0, 1) * 100).round(3)
    if "Cost ($)" in agg.columns and "Clicks" in agg.columns:
        agg["Avg CPC"] = (agg["Cost ($)"] / agg["Clicks"].replace(0, 1)).round(2)
    if "Cost ($)" in agg.columns and "Conversions" in agg.columns:
        agg["CPA ($)"] = (agg["Cost ($)"] / agg["Conversions"].replace(0, 1)).round(2)
    if "Conversions" in agg.columns and "Clicks" in agg.columns:
        agg["Conversion Rate"] = (agg["Conversions"] / agg["Clicks"].replace(0, 1) * 100).round(2)

    return agg


def _build_context(question: str) -> str:
    """
    Build the data context block for the prompt.
    Broad keyword matching so time/period/general questions load all relevant data.
    Applies date filtering when the user asks for a specific date range.
    """
    q = question.lower()

    # ── Detect date filter from question ─────────────────────────────────────
    date_filter = _parse_date_filter(question)
    date_label = ""
    if date_filter:
        date_label = f" (filtered: {date_filter[0]} to {date_filter[1]})"

    # Expanded keyword routing — covers time references, synonyms, comparisons
    slot_keywords = {
        "campaigns_df": [
            "campaign", "spend", "budget", "cost", "ctr", "cpa", "roas",
            "conversion rate", "impression", "click", "performance", "ad group",
            "last 7 days", "last 30 days", "last week", "last month", "yesterday",
            "7 days", "30 days", "week", "month", "period", "date range",
            "overview", "summary", "all", "everything", "account", "overall",
            "top", "best", "worst", "high", "low", "increase", "decrease",
            "compare", "trend", "change", "revenue", "data", "need",
        ],
        "keywords_df": [
            "keyword", "quality score", "qs", "match type", "cpc", "bid",
            "exact", "phrase", "broad", "negative keyword", "search query",
            "ad relevance", "expected ctr", "landing page experience",
        ],
        "geo_df": [
            "geo", "location", "city", "country", "region", "state",
            "india", "delhi", "mumbai", "bangalore", "where", "audience",
            "geographic", "area", "territory",
        ],
        "search_terms_df": [
            "search term", "search query", "what people search", "queries",
            "search", "intent",
        ],
        "hourly_df": [
            "hour", "hourly", "day of week", "time of day", "schedule",
            "daypart", "morning", "evening", "weekend", "weekday",
            "when", "peak", "off-peak", "ad schedule", "time",
        ],
        "lp_df": [
            "landing page", "url", "website", "page", "lp", "destination",
        ],
    }

    # Collect all matching slots
    relevant = []
    for slot, kws in slot_keywords.items():
        if any(kw in q for kw in kws):
            if slot not in relevant:
                relevant.append(slot)

    # If nothing matched at all, load campaigns + keywords as a safe default
    if not relevant:
        relevant = ["campaigns_df", "keywords_df"]

    # Load all slots for truly broad questions
    broad_terms = [
        "everything", "all", "overview", "summary", "report", "account",
        "overall", "full", "complete", "analyse", "analyze",
    ]
    if any(t in q for t in broad_terms):
        relevant = _all_slot_keys()

    # Build context string — only include non-empty DFs
    context_parts = []
    for slot in relevant:
        df = _get_df(slot)
        if df is not None and not df.empty:
            label = slot.replace("_df", "").replace("_", " ").title()

            # Apply date filtering if user asked for a date range
            if date_filter and "Date" in df.columns:
                start_d, end_d = date_filter
                df_filtered = _filter_df_by_dates(df, start_d, end_d)

                if df_filtered is not None and not df_filtered.empty:
                    # Aggregate daily rows into per-campaign summary
                    if slot == "campaigns_df":
                        df_display = _aggregate_filtered_campaigns(df_filtered)
                    else:
                        df_display = df_filtered

                    context_parts.append(
                        f"### {label} Data{date_label} (showing up to 50 rows)\n"
                        + df_display.head(50).to_string(index=False)
                    )
                else:
                    context_parts.append(
                        f"### {label} Data\n"
                        f"No data found for the requested date range ({start_d} to {end_d}). "
                        f"Data may only cover a different period."
                    )
            else:
                # No date filter or no Date column — show data as-is
                # If campaigns has Date column but no filter requested, aggregate it
                if "Date" in df.columns and slot == "campaigns_df":
                    df_display = _aggregate_filtered_campaigns(df)
                else:
                    df_display = df
                context_parts.append(
                    f"### {label} Data (showing up to 50 rows)\n"
                    + df_display.head(50).to_string(index=False)
                )

    # Absolute fallback: load whatever is available
    if not context_parts:
        for slot in _all_slot_keys():
            df = _get_df(slot)
            if df is not None and not df.empty:
                label = slot.replace("_df", "").replace("_", " ").title()
                context_parts.append(
                    f"### {label} Data (showing up to 50 rows)\n"
                    + df.head(50).to_string(index=False)
                )
                break  # at least load one dataset

    return "\n\n".join(context_parts)


def _note_about_data() -> str:
    """Return a note about what data is actually available."""
    available = []
    for slot in _all_slot_keys():
        df = _get_df(slot)
        if df is not None and not df.empty:
            available.append(slot.replace("_df", "").replace("_", " ").title())
    if available:
        return f"Note: Available datasets are: {', '.join(available)}."
    return ""


def answer_question(question: str, conversation_history: list = None) -> str:
    """
    Core function. Takes a question, returns a clean markdown answer string.
    conversation_history: list of {"role": "user"/"assistant", "content": "..."}

    The AI has full awareness of the loaded project data and answers ANY
    question about campaigns, keywords, geo, hourly performance, landing pages,
    strategy, recommendations, trends — no blocking on date ranges or topics.
    """
    context = _build_context(question)
    data_note = _note_about_data()

    if not context:
        return (
            "⚠️ **No data loaded yet.** Please generate a report first from the home page.\n\n"
            "Once generated, I'll have full access to your campaign data and can answer "
            "any question — campaigns, keywords, spend, locations, timings, strategy, and more."
        )

    # Build conversation history block (last 8 turns = 4 exchanges)
    history_block = ""
    if conversation_history:
        for turn in conversation_history[-8:]:
            role = "User" if turn["role"] == "user" else "Assistant"
            content = str(turn.get("content", ""))[:600]
            history_block += f"\n{role}: {content}"

    # Check if date filtering was applied
    date_filter = _parse_date_filter(question)
    date_context_note = ""
    if date_filter:
        date_context_note = (
            f"\nIMPORTANT: The data below has ALREADY been filtered to the date range "
            f"{date_filter[0]} to {date_filter[1]} as the user requested. "
            f"Present this data directly and confidently. Do NOT say you cannot provide "
            f"data for this period — the filtering has already been done for you."
        )

    prompt = f"""You are a senior Google Ads performance expert. You have FULL ACCESS to this
client's Google Ads account data (loaded below) and you answer EVERY question fully.

{data_note}
{date_context_note}

ABSOLUTE RULES:
• NEVER say "I can't provide data for that date range" — the data below is already
  filtered to whatever the user asked for. Just present it.
• NEVER refuse to answer. NEVER redirect users to "generate a new report".
• If the data below is empty or missing for a specific metric, say what IS available
  and provide insights from that.
• Answer ANY question: campaigns, keywords, spend, geo, hourly, strategy, trends,
  comparisons, recommendations, budget allocation, bid adjustments — everything.

FORMATTING RULES:
• Use **bold** for campaign names, keywords, and important numbers
• Use bullet points (•) for lists; use numbered lists for ranked items
• Use a markdown table when comparing 3+ items side-by-side
• Always include a 🔑 **Key Insight:** line with the single most important finding
• Cite real values from the data (e.g. "CTR: 3.2%", "Cost: $12,450", "CPA: $4.20")
• Keep responses focused but complete — aim for 200–500 words

{f"Conversation history:{history_block}" if history_block else ""}

User question: {question}

=== Google Ads Account Data ===
{context}
"""

    try:
        response = model.generate_content(prompt)
        text = getattr(response, "text", None)
        if not text:
            return "⚠️ The AI returned an empty response. Please rephrase your question."
        return text.strip()
    except Exception as e:
        err = str(e)
        if "429" in err or "quota" in err.lower() or "rate" in err.lower():
            return (
                "⚠️ **Rate limit hit.** The AI provider is temporarily throttled. "
                "Please wait 10–15 seconds and try again."
            )
        return (
            f"⚠️ **Error generating response:** {err}\n\n"
            "Please try rephrasing your question or try again in a moment."
        )



# ─────────────────────────────────────────────────────────────────────────────
# FLASK Blueprint — add to your existing Flask app
# ─────────────────────────────────────────────────────────────────────────────

def create_flask_blueprint():
    """Returns a Flask Blueprint with /  (chat page) and /ask (POST endpoint)."""
    try:
        from flask import Blueprint, request, jsonify, render_template_string
    except ImportError:
        print("[CHAT] Flask not available")
        return None

    chat_bp = Blueprint("chat", __name__)

    CHAT_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>💬 Chat with Your Google Ads Data</title>
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/dompurify@3.1.6/dist/purify.min.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#f0f2f5;display:flex;flex-direction:column;height:100vh;overflow:hidden}
.hdr{background:#1a73e8;color:#fff;padding:14px 20px;display:flex;align-items:center;gap:10px;box-shadow:0 2px 6px rgba(0,0,0,.2);flex-shrink:0}
.hdr h1{font-size:17px;font-weight:600}
.hdr small{font-size:12px;opacity:.8}
.hdr-links{margin-left:auto;display:flex;gap:14px}
.hdr-links a{color:#fff;font-size:12px;text-decoration:none;opacity:.85}
.hdr-links a:hover{opacity:1;text-decoration:underline}
.chat{flex:1;overflow-y:auto;padding:16px 20px;max-width:900px;width:100%;margin:0 auto}
.msg{margin-bottom:16px;display:flex;gap:10px}
.msg.user{flex-direction:row-reverse}
.av{width:32px;height:32px;border-radius:50%;flex-shrink:0;display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:700;margin-top:2px}
.av.u{background:#1a73e8;color:#fff}.av.b{background:#34a853;color:#fff}
.bbl{max-width:82%;padding:12px 16px;border-radius:16px;font-size:14px;line-height:1.7}
.user .bbl{background:#1a73e8;color:#fff;border-bottom-right-radius:4px}
.bot .bbl{background:#fff;color:#202124;border-bottom-left-radius:4px;box-shadow:0 1px 4px rgba(0,0,0,.12)}
.thinking .bbl{background:#f8f9fa;color:#5f6368;font-style:italic}
/* Markdown rendering inside bot bubbles */
.bot .bbl h1,.bot .bbl h2,.bot .bbl h3{margin:10px 0 6px;font-size:15px;color:#1a1a1a}
.bot .bbl p{margin-bottom:8px}
.bot .bbl ul,.bot .bbl ol{margin:6px 0 8px 18px}
.bot .bbl li{margin-bottom:4px}
.bot .bbl strong{color:#1a1a1a;font-weight:700}
.bot .bbl code{background:#f1f3f4;padding:2px 6px;border-radius:4px;font-size:13px;font-family:monospace}
.bot .bbl pre{background:#f1f3f4;padding:10px;border-radius:8px;overflow-x:auto;margin:8px 0}
.bot .bbl table{border-collapse:collapse;width:100%;max-width:100%;margin:10px 0;font-size:13px;table-layout:auto;display:block;overflow:auto}
.bot .bbl th,.bot .bbl td{padding:7px 10px;border-bottom:1px solid #e8eaed;white-space:normal;word-break:break-word}
.bot .bbl th{background:#1a73e8;color:#fff;text-align:left}
.bot .bbl tr:nth-child(even) td{background:#f8f9fa}
.bot .bbl blockquote{border-left:3px solid #1a73e8;padding-left:10px;color:#5f6368;margin:8px 0}
/* Input area */
.inp{background:#fff;border-top:1px solid #e0e0e0;padding:12px 20px 14px;flex-shrink:0}
.inp-inner{max-width:900px;margin:0 auto}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px}
.chip{background:#e8f0fe;color:#1a73e8;border:1px solid #c5d8fe;border-radius:20px;padding:5px 12px;font-size:12px;cursor:pointer;white-space:nowrap;transition:.15s}
.chip:hover{background:#d2e3fc;border-color:#93b4fa}
.row{display:flex;gap:8px;align-items:flex-end}
textarea{flex:1;border:1.5px solid #dadce0;border-radius:12px;padding:10px 14px;font-size:14px;resize:none;outline:none;font-family:inherit;max-height:120px;line-height:1.5;transition:.2s}
textarea:focus{border-color:#1a73e8;box-shadow:0 0 0 3px rgba(26,115,232,.1)}
#btn{background:#1a73e8;color:#fff;border:none;border-radius:12px;padding:10px 20px;font-size:14px;cursor:pointer;font-weight:600;white-space:nowrap;transition:.15s}
#btn:hover{background:#1557b0}
#btn:disabled{background:#bdc1c6;cursor:not-allowed}
.footer{display:flex;justify-content:space-between;align-items:center;margin-top:6px}
#status{font-size:11px;color:#80868b}
a.back{font-size:12px;color:#1a73e8;text-decoration:none}
a.back:hover{text-decoration:underline}
</style>
</head>
<body>
<div class="hdr">
  <div>💬</div>
  <div><h1>Chat with Your Google Ads Data</h1><small>Ask anything — campaigns, keywords, spend, geo, hours</small></div>
  <div class="hdr-links"><a href="/report">📊 Report</a><a href="/">🏠 Home</a></div>
</div>
<div class="chat" id="chat">
  <div class="msg bot"><div class="av b">AI</div>
  <div class="bbl"><p>👋 Hi! I have your Google Ads data ready. Here are a few things you can ask:</p>
  <ul><li>Which campaigns are wasting the most budget?</li><li>What time of day converts best?</li><li>Which keywords have a low Quality Score?</li><li>Why is my CPA high?</li><li>Show me top-performing locations</li></ul>
  <p><em>Note: Data covers the last 30 days from when the report was generated.</em></p>
  </div></div>
</div>
<div class="inp">
  <div class="inp-inner">
    <div class="chips">
      <span class="chip" onclick="ask('Which campaigns waste the most budget?')">💸 Wasted budget?</span>
      <span class="chip" onclick="ask('What hours and days convert best?')">⏰ Best time to show ads?</span>
      <span class="chip" onclick="ask('Show keywords with low Quality Score')">🔑 Low QS keywords?</span>
      <span class="chip" onclick="ask('Top converting locations?')">🌍 Top locations?</span>
      <span class="chip" onclick="ask('Why is my CPA high and how do I fix it?')">📉 High CPA?</span>
      <span class="chip" onclick="ask('Show zero-conversion keywords with spend')">🗑 Zero-conv keywords?</span>
      <span class="chip" onclick="ask('Give me a full account performance summary')">📊 Account summary</span>
      <span class="chip" onclick="ask('Which campaigns have the best ROAS or conversion rate?')">🏆 Best campaigns?</span>
    </div>
    <div class="row">
      <textarea id="q" placeholder="Ask anything about your Google Ads data…" rows="1"
        onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();send()}"
        oninput="this.style.height='auto';this.style.height=Math.min(this.scrollHeight,120)+'px'"></textarea>
      <button id="btn" onclick="send()">Send ➤</button>
    </div>
    <div class="footer">
      <div id="status"></div>
      <a class="back" href="/report">← Back to Report</a>
    </div>
  </div>
</div>
<script>
// Configure marked for safe rendering
marked.setOptions({breaks:true,gfm:true});

const history=[];

function renderMarkdown(text){
  try{
    const raw=marked.parse(text||'');
    return typeof DOMPurify!=='undefined'?DOMPurify.sanitize(raw):raw;
  }catch(e){
    // fallback: escape HTML and show as-is
    return text.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/\\n/g,'<br>');
  }
}

function addMsg(role,text,isHtml){
  const c=document.getElementById('chat');
  const d=document.createElement('div');
  const bot=role==='bot'||role==='thinking';
  d.className='msg '+role;
  const avLabel=bot?'AI':'You';
  const avClass=bot?'b':'u';
  const bubble=document.createElement('div');
  bubble.className='bbl';
  if(bot&&!isHtml&&role==='bot'){
    bubble.innerHTML=renderMarkdown(text);
  } else if(isHtml){
    bubble.innerHTML=text;
  } else {
    bubble.textContent=text;
  }
  const av=document.createElement('div');
  av.className='av '+avClass;
  av.textContent=avLabel;
  d.appendChild(av);
  d.appendChild(bubble);
  c.appendChild(d);
  c.scrollTop=c.scrollHeight;
  return d;
}

function ask(t){document.getElementById('q').value=t;send();}

async function send(){
  const inp=document.getElementById('q'),btn=document.getElementById('btn');
  const q=inp.value.trim();if(!q)return;
  inp.value='';inp.style.height='auto';btn.disabled=true;
  document.getElementById('status').textContent='';
  addMsg('user',q);
  const th=addMsg('thinking','⏳ Analysing your data…',true);
  const t0=Date.now();
  try{
    const r=await fetch('/chat/ask',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({question:q,history:history})
    });
    if(!r.ok){
      th.remove();
      addMsg('bot','⚠️ Server error '+r.status+' — please refresh and try again.');
      btn.disabled=false;
      return;
    }
    const data=await r.json();
    th.remove();
    addMsg('bot',data.answer||'(no response)');
    const elapsed=((Date.now()-t0)/1000).toFixed(1);
    const provider=data.provider||'AI';
    document.getElementById('status').textContent='Answered in '+elapsed+'s via '+provider;
    history.push({role:'user',content:q},{role:'assistant',content:data.answer||''});
    if(history.length>20)history.splice(0,2);
  }catch(e){
    th.remove();
    addMsg('bot','⚠️ Network error: '+e.message);
  }
  btn.disabled=false;
  inp.focus();
}
</script>
</body>
</html>"""

    @chat_bp.route("/", methods=["GET"])
    def chat_page():
        return render_template_string(CHAT_HTML)

    @chat_bp.route("/ask", methods=["POST"])
    def chat_ask():
        try:
            data = request.get_json(force=True, silent=True) or {}
            question = str(data.get("question", "")).strip()
            history  = data.get("history", [])
            if not question:
                return jsonify({"answer": "Please type a question.", "provider": ""})
            answer = answer_question(question, history)
            provider = "AI"
            try:
                if hasattr(model, "_providers") and model._providers:
                    provider = model._providers[0][0]
            except Exception:
                pass
            return jsonify({"answer": answer, "provider": provider})
        except Exception as e:
            import traceback
            print(f"[CHAT] /ask error: {e}\n{traceback.format_exc()}")
            return jsonify({"answer": f"Server error: {e}", "provider": "error"}), 500

    return chat_bp




# ── Module-level blueprint (imported by app.py) ───────────────────────────────
try:
    chat_bp = create_flask_blueprint()
    if chat_bp is None:
        raise RuntimeError("Blueprint creation returned None")
    print("[CHAT] ✅ Blueprint ready at /chat")
except Exception as _bp_err:
    print(f"[CHAT] Blueprint init error: {_bp_err}")
    chat_bp = None