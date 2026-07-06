"""
NLP Complaint Analysis Platform — Backend API
Phase 4: Semantic Clustering + Urgency Triage + Reopen Risk
KESCO / DVVNL Power Utility, Kanpur

Datasets (expected inside backend/data/):
  - helpdesk_clean.csv                                            -> dataset_one  (application complaints)
  - supply_clean.csv                                              -> dataset_two  (breakdown/supply complaints, 2025)
  - BREAKDOWN SHUTDOWN& SUPPLY RELATED SUMMARY FROM 01-JAN 2026 TO 31-MAY-2026.xlsx -> dataset_three (substation summary)
  - KESCO RE-OPEN DATA 19-JUN'26.xlsx                              -> reopened     (reopened complaints, 2026)

Join strategy note (IMPORTANT — read before changing reopen logic):
  supply_clean.csv contains 2025 complaint numbers (e.g. KS11042501136) and the
  reopened file contains 2026 complaint numbers (e.g. KS01042602168). There is
  ZERO overlap on COMPLAINT_NO between the two files, so a direct row-level
  join is not possible.

  Beyond that, the two files are different COMPLAINT DOMAINS:
    - supply_clean.COMPLAINT_TYPE is a constant ("SUPPLY RELATED") and its
      finer column supply_subtype_std only has 3 coarse values (NO_SUPPLY,
      VOLTAGE_ISSUE, UG_CABLE_FAULT) — not the 19-category fault taxonomy.
    - reopened.COM_TYPE_NAME is billing/service/connection categories
      (smart meter, new connection, payment, bill revision, etc.) — these
      are NOT power-supply fault complaints at all.
  Because of this, there is NO valid way to compute a "reopen rate per
  fault category" from these two files — the vocabularies don't describe
  the same thing, so any type-level join would be invented, not measured.

  What IS valid: reopen rates calculated by geography (SUBSTATION /
  SUBDIVISION / DIVISION), since both files share those columns and a
  high rate of repeat/reopened complaints from one area is a legitimate
  signal regardless of complaint type. These are exposed as
  GENERAL_REOPEN_RATE_BY_* — explicitly NOT fault-specific — and should
  never be presented to users as "fault reopen rate" or similar.

Urgency classification — two-tier design:
  /analyse uses rule_based_urgency() as a cheap first-pass keyword scan,
  then hands control to the LLM for a second opinion. The LLM can override
  the keyword result if the full text context warrants it.

  /batch-triage uses triage_urgency() — keyword scan first, then embeddings
  reference example sentences rather than exact keyword matching. This
  catches paraphrases and Hinglish variants (e.g. "transformer jal gaya
  hai") that exact keyword lists can never fully cover, without needing
  an LLM call per complaint (which would be too slow and expensive for
  batches of hundreds).

  CRITICAL_KEYWORDS / HIGH_KEYWORDS are kept for /analyse's rule-based
  pre-check only. CRITICAL_REFERENCE_EXAMPLES / HIGH_REFERENCE_EXAMPLES
  are the embedding-based counterparts used by /batch-triage.
"""

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from groq import Groq
from sentence_transformers import SentenceTransformer
import numpy as np
import re
import json
import os
import io
import uuid
import pandas as pd
from typing import Optional
from datetime import datetime

import matplotlib
matplotlib.use("Agg")  # headless — no display server in a backend process
import matplotlib.pyplot as plt

from docx import Document
from docx.shared import Inches, Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(title="NLP Complaint Analysis API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

client     = Groq()  # reads GROQ_API_KEY from environment (loaded via .env above)
GROQ_MODEL = "llama-3.3-70b-versatile"

pd.set_option("future.no_silent_downcasting", True)

# ---------------------------------------------------------------------------
# Domain constants — derived from dataset_three breakdown summary
# ---------------------------------------------------------------------------

FAULT_CATEGORIES = [
    "11KV Line Fault", "33KV Line Fault", "DT Damaged/DT Fuse Burnt/Fire on DT",
    "DT Maintenance", "Fire in Control Room/Switchyard", "Line Maintenance",
    "LT/ABC Line Fault", "Other Transient Fault", "Overloading",
    "Power Transformer Fault", "Rostering",
    "Shutdown for External Agency Infrastructure Works",
    "Storm/Lightning/Heavy Rain", "Substation Maintenance",
    "System Improvement Work/Deposit Works", "Transformer Fault",
    "Transformer Maintenance", "Transmission Rostering",
    "Supply Related (Miscellaneous)",
]

SEMANTIC_CLUSTERS = {
    "Line Faults": ["11KV Line Fault", "33KV Line Fault", "LT/ABC Line Fault"],
    "Transformer Issues": [
        "DT Damaged/DT Fuse Burnt/Fire on DT", "Power Transformer Fault",
        "Transformer Fault", "Transformer Maintenance", "DT Maintenance",
    ],
    "Supply Quality": [
        "Overloading", "Other Transient Fault",
        "Supply Related (Miscellaneous)", "Transmission Rostering",
    ],
    "Planned Operations": [
        "Line Maintenance", "Substation Maintenance", "Rostering",
        "Shutdown for External Agency Infrastructure Works",
        "System Improvement Work/Deposit Works",
    ],
    "Environmental / External": [
        "Storm/Lightning/Heavy Rain", "Fire in Control Room/Switchyard",
    ],
}

# Reverse lookup: fault_category (uppercased) -> cluster_name. Built once so
# any row's existing category value (supply_subtype_std / canonical_category /
# COMPLAINT_TYPE / etc.) can be assigned to one of the five clusters without
# re-deriving this mapping every time a file is uploaded.
_CATEGORY_TO_CLUSTER = {
    cat.strip().upper(): cluster
    for cluster, cats in SEMANTIC_CLUSTERS.items()
    for cat in cats
}


def _category_to_cluster(raw_category) -> str:
    """
    Map a raw category string (from an uploaded file's existing_category
    column) to one of the five SEMANTIC_CLUSTERS buckets. Falls back to
    'Unclassified' rather than guessing — a wrong cluster assignment is
    worse than an honest 'we don't know' bucket the user can see and decide
    what to do with.
    """
    if raw_category is None or (isinstance(raw_category, float) and pd.isna(raw_category)):
        return "Unclassified"
    key = str(raw_category).strip().upper()
    return _CATEGORY_TO_CLUSTER.get(key, "Unclassified")

# ---------------------------------------------------------------------------
# Exact-keyword lists — used ONLY by rule_based_urgency() in /analyse.
# /batch-triage uses triage_urgency() instead (see below).
#
# Includes burn-related Hindi/Hinglish terms ("jal gaya", "phat gaya", etc.)
# which are common ways to report transformer/DT fire/damage and were
# previously missing, causing "transformer jal gaya hai" to score LOW.
# ---------------------------------------------------------------------------

CRITICAL_KEYWORDS = [
    # Fire / active flame
    "fire", "aag", "chingari", "sparking",
    # Explosion / blast
    "blast", "dhamaka", "phat gaya", "phat gayi", "phat raha",
    # Burning / burnt — very commonly used in Hindi/Hinglish for transformer/DT damage
    "smoke", "dhuaan",
    "jal gaya", "jal gayi", "jal raha", "jal rahi", "jala hua", "jal kar",
    "burnt", "burning", "burst",
    # Electrocution / shock
    "electrocution", "karnt", "current laga", "shock laga", "bijli ka jhatka",
    # Infrastructure down
    "wire down", "taar giri", "taar toot", "pole fallen", "khamba gira",
    # Casualties
    "death", "maut", "mrityu", "accident", "ghayal", "injured",
]

HIGH_KEYWORDS = [
    "hospital", "aspatal", "overloading", "transformer hot", "garam",
    "buzzing", "24 ghante", "30 ghante", "48 ghante",
    "water supply", "paani nahi", "diesel khatam",
]

# ---------------------------------------------------------------------------
# Embedding-based urgency reference sentences.
# Used by /batch-triage via triage_urgency() (Stage 2 only).
#
# These are natural-language example sentences, NOT bare keywords. Embedding
# models need full phrasing to capture meaning in context; isolated words
# like "fire" don't encode the same semantic neighbourhood as a sentence
# describing a fire at a transformer. Sentences cover Hindi, English, and
# Hinglish variants so the multilingual model can align them properly.
#
# Threshold tuning note:
#   CRITICAL_THRESHOLD = 0.55 — deliberately permissive to avoid missing
#   genuine safety complaints. A false positive (routine complaint flagged
#   CRITICAL) is far less harmful than a false negative (actual fire/
#   electrocution complaint missed). Field teams will triage from the CRITICAL
#   queue anyway; false positives add a few extra reviews, not harm.
#
#   HIGH_THRESHOLD = 0.50 — similarly permissive for the same reason.
#   LOW is the fallback: anything below both thresholds.
# ---------------------------------------------------------------------------

CRITICAL_REFERENCE_EXAMPLES = [
    # Fire / flame at transformer or DT
    "transformer mein aag lag gayi hai",
    "transformer jal gaya hai",
    "DT jal gaya poori gali andhere mein",
    "bijli ke khambhe mein aag lagi hai",
    "meter phat gaya aur aag lag gayi",
    # Sparking / live wire hazard
    "taar mein sparking ho rahi hai",
    "wire mein chingari nikal rahi hai",
    "khule taar mein bijli daud rahi hai",
    # Explosion / burst
    "blast ho gaya substation mein",
    "transformer phat gaya bahut tez awaaz aayi",
    # Smoke / burning smell
    "transformer se dhuaan nikal raha hai",
    "substation mein dhuaan aur jalne ki badboo aa rahi hai",
    # Electrocution / shock
    "bijli ka jhatka laga koi ghayal hua hai",
    "current lagne se kisi ko chot lagi",
    "someone got electric shock from the line",
    # Infrastructure fallen
    "taar toot kar sadak par gir gaya hai",
    "khamba gir gaya hai bijli ke taar ke saath",
    "live wire is lying on the road",
    # Casualty
    "is accident mein kisi ki maut ho gayi",
    "ek aadmi bijli se mar gaya",
]

HIGH_REFERENCE_EXAMPLES = [
    # Hospital / critical services without power
    "hospital mein bijli nahi hai kai ghanton se",
    "aspatal ke paas se supply gayab hai",
    "dialysis center mein light nahi hai",
    # Overloading / transformer stress
    "transformer bahut garam ho raha hai aur buzzing ki awaaz aa rahi hai",
    "DT mein overloading ho rahi hai",
    "transformer se aanch aa rahi hai",
    # Extended outage (24h+)
    "24 ghante se bijli nahi aayi hamare ilake mein",
    "30 ghante se supply band hai",
    "48 hours no electricity in our area",
    # Water / essential services affected
    "paani ka motor nahi chal raha bijli na hone ki wajah se",
    "water supply thap ho gayi bijli nahi hone se",
    # Generator / diesel emergency
    "generator ka diesel khatam hone wala hai",
    "backup power khatam ho raha hai hospital mein",
]

LOW_REFERENCE_EXAMPLES = [
    "meter ki reading sahi nahi aa rahi",
    "bill bahut zyada aaya hai is mahine",
    "naya connection lena hai",
    "meter badalna hai purana ho gaya hai",
    "payment ki receipt nahi mili",
    "address correction karna hai mere account mein",
    "bijli thodi der ke liye gayi thi ab aa gayi hai",
    "voltage thodi kam hai fan slow chal raha hai",
]

# ---------------------------------------------------------------------------
# Data loading helpers
# ---------------------------------------------------------------------------

def _safe_read(path: str, **kwargs) -> pd.DataFrame:
    """Read CSV or Excel. Returns empty DataFrame on failure (logs why)."""
    try:
        if path.endswith(".xlsx") or path.endswith(".xls"):
            df = pd.read_excel(path, **kwargs)
        else:
            df = pd.read_csv(path, **kwargs)
        print(f"\u2713 Loaded {os.path.basename(path)}: {len(df)} rows")
        return df
    except Exception as e:
        print(f"\u26a0  Could not load {path}: {e}")
        return pd.DataFrame()


def _norm(series: pd.Series) -> pd.Series:
    """Normalise a string series: strip whitespace, uppercase."""
    return series.astype(str).str.strip().str.upper()


def _reopen_rate(total_series: pd.Series, reopen_series: pd.Series) -> pd.Series:
    """
    Given two groupby-size series, return a reopen-rate-% series covering
    every key present in total_series. Keys with no matching reopen rows
    get a genuine 0.0% rather than being dropped — a location with zero
    reopens should show as 0%, not disappear from the results.
    """
    stats = pd.concat(
        [total_series.rename("total"), reopen_series.rename("reopen")], axis=1
    )
    stats["reopen"] = stats["reopen"].fillna(0)
    stats = stats.dropna(subset=["total"])  # only drop if the LOCATION itself is unknown
    return (stats["reopen"] / stats["total"] * 100).round(1)

# ---------------------------------------------------------------------------
# Load raw files
#
# IMPORTANT: adjust these filenames/paths if yours differ. Place all four
# files inside backend/data/.
# ---------------------------------------------------------------------------

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

df1 = _safe_read(os.path.join(DATA_DIR, "helpdesk_clean.csv"))
df2 = _safe_read(os.path.join(DATA_DIR, "supply_clean.csv"))
df3 = _safe_read(os.path.join(
    DATA_DIR,
    "BREAKDOWN SHUTDOWN& SUPPLY RELATED SUMMARY FROM 01-JAN 2026 TO 31-MAY-2026.xlsx",
))
df_reopen = _safe_read(os.path.join(
    DATA_DIR,
    "KESCO RE-OPEN DATA 19-JUN'26.xlsx",
))

# ---------------------------------------------------------------------------
# Computed dicts (populated below if data loaded OK)
# ---------------------------------------------------------------------------

REOPEN_BY_SUBSTATION:       dict = {}   # ALL substations with a computable rate
REOPEN_BY_SUBSTATION_TOP10: dict = {}   # worst 10, for hotspot-style reporting
REOPEN_BY_SUBDIVISION:      dict = {}
REOPEN_BY_DIVISION:         dict = {}
REOPEN_DESK_DIST:           dict = {}   # REOPEN_LEVEL is an escalation desk code, not a severity tier
REOPENED_COMPLAINT_TYPES:   list = []   # billing/service categories — kept for transparency
REOPENED_SUBTYPES:          list = []
REOPEN_SIGNAL_PHRASES:      list = [
    "pehle bhi aayi thi", "baar baar", "dobara fault",
    "phir se", "wapas aayi", "not fixed", "same problem",
    "second time", "previously complained", "supply restored",
    "fault rectified", "temporary repair", "partial repair",
]

FEW_SHOT_EXAMPLES: str = ""

# ---------------------------------------------------------------------------
# Build GENERAL complaint reopen rates — geography only.
# ---------------------------------------------------------------------------

if not df2.empty and not df_reopen.empty:

    # -- Reopen rate by SUBSTATION (ALL substations, not just worst 10) ---
    df2["_sub"]       = _norm(df2.get("SUBSTATION",       pd.Series(dtype=str)))
    df_reopen["_sub"] = _norm(df_reopen.get("SUBSTATION", pd.Series(dtype=str)))

    REOPEN_BY_SUBSTATION = (
        _reopen_rate(df2.groupby("_sub").size(), df_reopen.groupby("_sub").size())
        .to_dict()
    )
    REOPEN_BY_SUBSTATION_TOP10 = dict(
        sorted(REOPEN_BY_SUBSTATION.items(), key=lambda x: -x[1])[:10]
    )
    print(f"\u2713 Reopen rates by substation:  {len(REOPEN_BY_SUBSTATION)} total, "
          f"top-10 worst: {list(REOPEN_BY_SUBSTATION_TOP10.keys())[:3]}...")

    # -- Reopen rate by SUBDIVISION ----------------------------------------
    df2["_subdiv"]       = _norm(df2.get("SUBDIVISION",       pd.Series(dtype=str)))
    df_reopen["_subdiv"] = _norm(df_reopen.get("SUBDIVISION", pd.Series(dtype=str)))

    REOPEN_BY_SUBDIVISION = (
        _reopen_rate(df2.groupby("_subdiv").size(), df_reopen.groupby("_subdiv").size())
        .sort_values(ascending=False)
        .to_dict()
    )
    print(f"\u2713 Reopen rates by subdivision: {len(REOPEN_BY_SUBDIVISION)} matched")

    # -- Reopen rate by DIVISION -------------------------------------------
    df2["_div"]       = _norm(df2.get("DIVISION",       pd.Series(dtype=str)))
    df_reopen["_div"] = _norm(df_reopen.get("DIVISION", pd.Series(dtype=str)))

    REOPEN_BY_DIVISION = (
        _reopen_rate(df2.groupby("_div").size(), df_reopen.groupby("_div").size())
        .sort_values(ascending=False)
        .to_dict()
    )
    print(f"\u2713 Reopen rates by division:    {len(REOPEN_BY_DIVISION)} matched")

    print("\u2713 Skipping fault-type reopen rate: supply_clean and reopened "
          "datasets are different complaint domains (breakdown vs. billing/service)")

# ---------------------------------------------------------------------------
# Reopen desk/escalation distribution
# ---------------------------------------------------------------------------

if not df_reopen.empty and "REOPEN_LEVEL" in df_reopen.columns:
    REOPEN_DESK_DIST = df_reopen["REOPEN_LEVEL"].value_counts().to_dict()
    print(f"\u2713 Reopen escalation desk distribution: {REOPEN_DESK_DIST}")

# ---------------------------------------------------------------------------
# Unique complaint types and subtypes from reopened dataset
# ---------------------------------------------------------------------------

if not df_reopen.empty:
    if "COM_TYPE_NAME" in df_reopen.columns:
        REOPENED_COMPLAINT_TYPES = df_reopen["COM_TYPE_NAME"].dropna().unique().tolist()
    if "COM_SUB_TYPE_NAME" in df_reopen.columns:
        REOPENED_SUBTYPES = df_reopen["COM_SUB_TYPE_NAME"].dropna().unique().tolist()

# ---------------------------------------------------------------------------
# Extend HIGH_KEYWORDS with reopen signal phrases (for /analyse rule scan)
# ---------------------------------------------------------------------------

HIGH_KEYWORDS.extend(REOPEN_SIGNAL_PHRASES)

# ---------------------------------------------------------------------------
# Precompiled word-boundary patterns for every CRITICAL/HIGH keyword.
#
# Built once here (after both lists are final, including the reopen
# phrases just added above) rather than per-request — re.compile() per row
# would be wasteful at scale, and this list never changes after startup.
#
# \b...\b means the keyword must appear as its own word/phrase, not as a
# substring of a longer word. This prevents false positives like "aag"
# (fire) matching inside "vibhaag" (department) or "sambhaag" (division) —
# both common, unrelated words in Hindi government/utility text.
# ---------------------------------------------------------------------------
_KEYWORD_PATTERNS = {
    k: re.compile(r"\b" + re.escape(k) + r"\b")
    for k in set(CRITICAL_KEYWORDS) | set(HIGH_KEYWORDS)
}

# ---------------------------------------------------------------------------
# Few-shot examples from supply_clean (dataset_two) real remarks.
# ---------------------------------------------------------------------------

_remarks_col = next(
    (c for c in ["REMARKS", "STAFFREMARKS", "CLOSING_REMARKS"]
     if not df2.empty and c in df2.columns), None
)
_type_col = next(
    (c for c in ["supply_subtype_std", "fault_subtype_std", "COMPLAINT_TYPE"]
     if not df2.empty and c in df2.columns), None
)
if _remarks_col and _type_col:
    _ex = df2[[_remarks_col, _type_col]].dropna().head(30)
    FEW_SHOT_EXAMPLES = "\n".join(
        f'Complaint: "{r[_remarks_col]}" \u2192 Category: "{r[_type_col]}"'
        for _, r in _ex.iterrows()
    )
    print(f"\u2713 Built {len(_ex)} few-shot examples from supply_clean using '{_type_col}'")

# ---------------------------------------------------------------------------
# Load sentence-transformer embedding model + pre-compute reference embeddings.
#
# Model: paraphrase-multilingual-MiniLM-L12-v2
#   - Genuinely multilingual (50+ languages including Hindi)
#   - Small enough to run fast on CPU (~120 MB download, one-time)
#   - Strong at paraphrase detection — the key property we need for Hinglish
#     complaint triage
#
# Reference embeddings are computed ONCE at startup, not per request.
# Per-request cost is just: embed one complaint text + cosine similarity
# against ~35 reference vectors. This is typically <5ms on CPU.
#
# If the model fails to download (no internet / huggingface blocked), the
# system falls back to rule_based_urgency() for /batch-triage automatically,
# so startup won't crash — just a logged warning.
# ---------------------------------------------------------------------------

EMBEDDING_MODEL = None
HIGH_EMBEDDINGS     = None
LOW_EMBEDDINGS      = None

CRITICAL_THRESHOLD = 0.55   # permissive: false positives (extra reviews) < false negatives (missed fires)
HIGH_THRESHOLD     = 0.50

try:
    print("\u23f3 Loading sentence-transformer model (first run downloads ~120MB)...")
    EMBEDDING_MODEL = SentenceTransformer("paraphrase-multilingual-MiniLM-L12-v2")

    # CRITICAL tier is handled by keyword scan in triage_urgency() Stage 1,
    # so CRITICAL_EMBEDDINGS are not needed at inference time. Only HIGH and
    # LOW reference sets are embedded for the Stage 2 contextual comparison.
    HIGH_EMBEDDINGS = EMBEDDING_MODEL.encode(
        HIGH_REFERENCE_EXAMPLES, convert_to_numpy=True, normalize_embeddings=True
    )
    LOW_EMBEDDINGS = EMBEDDING_MODEL.encode(
        LOW_REFERENCE_EXAMPLES, convert_to_numpy=True, normalize_embeddings=True
    )
    print(f"\u2713 Embedding model loaded. Reference vectors: "
          f"{len(HIGH_REFERENCE_EXAMPLES)} high, {len(LOW_REFERENCE_EXAMPLES)} low "
          f"(CRITICAL tier handled by keyword scan, no embeddings needed).")
except Exception as e:
    print(f"\u26a0  Embedding model failed to load: {e}")
    print("   /batch-triage will fall back to keyword-based classification.")

print(f"\u2713 Startup complete \u2014 model: {GROQ_MODEL}")


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class ComplaintRequest(BaseModel):
    text:        str
    account_no:  Optional[str] = None
    substation:  Optional[str] = None
    subdivision: Optional[str] = None
    division:    Optional[str] = None


class BatchRequest(BaseModel):
    complaints: list[str]


class ClusterInsightRequest(BaseModel):
    cluster_name:      str
    share_pct:         float
    sample_complaints: Optional[list[str]] = None

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def rule_based_urgency(text: str) -> dict:
    """
    Word-boundary keyword scan (NOT plain substring match). Used as the
    pre-check injected into /analyse's LLM prompt. Also called as Stage 1
    of triage_urgency() below.

    Why word-boundary matters: a plain `"aag" in text` substring check also
    matches inside unrelated words like "vibhaag" (department) or
    "sambhaag" (division) — both common in Hindi government/utility text
    and completely unrelated to fire. \b...\b regex boundaries require the
    keyword to be its own word (or its own phrase, for multi-word keywords
    like "current laga"), not a substring of a longer word.
    """
    tl = text.lower()
    critical = [k for k in CRITICAL_KEYWORDS if _KEYWORD_PATTERNS[k].search(tl)]
    high     = [k for k in HIGH_KEYWORDS     if _KEYWORD_PATTERNS[k].search(tl)]
    if critical:
        return {"level": "CRITICAL", "triggers": critical, "queue": "bypass"}
    if high:
        return {"level": "HIGH",     "triggers": high,     "queue": "priority"}
    return      {"level": "LOW",     "triggers": [],       "queue": "standard"}


def triage_urgency(text: str) -> dict:
    """
    Two-stage urgency classifier used by /batch-triage.

    Stage 1 - keyword scan (near-zero cost):
        CRITICAL_KEYWORDS are exact safety signals (fire, sparking, jal gaya,
        electrocution, wire down, etc.). If any match, the complaint is
        immediately CRITICAL with no embedding needed. This is intentional:
        keyword hits are unambiguous, cheap, and should bypass any slower step.

    Stage 2 - contextual embedding (only if Stage 1 does NOT return CRITICAL):
        Complaints that pass the CRITICAL keyword filter are embedded and
        compared against HIGH and LOW reference sentence vectors via cosine
        similarity. This catches semantic HIGH cases (hospital without power,
        30-hour outage, overloaded transformer) that keyword lists cannot
        cover reliably without growing unmanageably long.

        If Stage 1 already returned HIGH (from HIGH_KEYWORDS), the embedding
        score can only confirm it - it cannot downgrade to LOW. This avoids
        false negatives on complaints that both a keyword and the embedding
        agree are high-priority.

        Falls back to the Stage 1 keyword result if the embedding model
        did not load (network issue on first run, HuggingFace blocked, etc.).

    This design means:
      - CRITICAL complaints skip embeddings entirely (fast path)
      - HIGH complaints get embedding confirmation but cannot be downgraded
      - LOW complaints go through embeddings to catch semantic HIGH cases
      - Only two reference sets (HIGH, LOW) are needed at inference time;
        CRITICAL_EMBEDDINGS are not used in /batch-triage since keywords
        already handle that tier definitively
    """
    # Stage 1: keyword scan
    keyword_result = rule_based_urgency(text)

    # CRITICAL from keywords -> done immediately, no embedding needed
    if keyword_result["level"] == "CRITICAL":
        keyword_result["method"] = "keyword"
        return keyword_result

    # Embedding model unavailable -> use keyword result as-is
    if EMBEDDING_MODEL is None:
        keyword_result["method"] = "keyword_fallback"
        return keyword_result

    # Stage 2: embed and compare against HIGH / LOW reference sets only
    # (CRITICAL tier is handled definitively by keywords above)
    vec = EMBEDDING_MODEL.encode(
        [text], convert_to_numpy=True, normalize_embeddings=True
    )[0]

    # Cosine similarity = dot product since vectors are unit-normalised
    max_high = float(np.max(HIGH_EMBEDDINGS @ vec))
    max_low  = float(np.max(LOW_EMBEDDINGS  @ vec))

    # Decision logic:
    #   keyword HIGH + embedding HIGH  -> HIGH  (both agree)
    #   keyword HIGH + embedding LOW   -> HIGH  (keyword wins, no downgrade)
    #   keyword LOW  + embedding HIGH  -> HIGH  (embedding catches what keyword missed)
    #   keyword LOW  + embedding LOW   -> LOW
    if keyword_result["level"] == "HIGH" or max_high >= HIGH_THRESHOLD:
        level, queue = "HIGH", "priority"
    else:
        level, queue = "LOW", "standard"

    return {
        "level":    level,
        "queue":    queue,
        "method":   "keyword+embedding",
        "triggers": keyword_result.get("triggers", []),
        "scores": {
            "high": round(max_high, 3),
            "low":  round(max_low,  3),
        },
    }


def keyword_scan_batch(texts: list[str]) -> list[dict]:
    """
    Stage-1-only scan: keyword matching for every row, NO embeddings at all.
    Used by /upload-complaints so the upload itself stays fast regardless
    of file size — embeddings are deferred entirely until a user applies
    filters in the Urgency Triage tab (see refine_urgency_for_indices()).

    needs_embedding is set to True ONLY for rows that are neither CRITICAL
    nor HIGH from keywords — i.e. only complaints that landed as LOW from
    the keyword scan, where the embedding step could potentially promote
    them to HIGH based on semantic meaning (e.g. "hospital mein light nahi"
    without an exact keyword hit). CRITICAL and HIGH keyword results are
    both definitive:
      - CRITICAL keywords are unambiguous safety signals; embeddings cannot
        add anything here and would only waste time.
      - HIGH keywords already flag the complaint as priority; the embedding
        step for HIGH rows can only confirm, never downgrade (same rule as
        the full pipeline), so there is no value in running it at upload
        time — the result would be HIGH either way.
    This means embeddings are computed only for the subset of complaints
    that are neither CRITICAL nor HIGH by keyword, which in a typical
    KESCO dataset is the large majority of routine LOW complaints, but the
    refinement only happens when a user actively filters in Urgency Triage,
    not at upload time.
    """
    results = []
    for t in texts:
        kw = rule_based_urgency(t)
        if kw["level"] in ("CRITICAL", "HIGH"):
            # Both are definitive from keywords — no embedding needed ever.
            results.append({**kw, "method": "keyword", "needs_embedding": False})
        else:
            # LOW provisional — embedding could promote to HIGH semantically.
            results.append({**kw, "method": "keyword_only_provisional", "needs_embedding": True})
    return results


def refine_urgency_for_indices(df: pd.DataFrame, text_col: str, indices, embedding_cache: dict):
    """
    Computes (and caches) embedding-based urgency ONLY for the given row
    indices of df — used when a user applies filters in Urgency Triage, so
    embedding work scales with the size of the filtered slice, not the
    whole uploaded file.

    embedding_cache is a dict on the upload record: {text: (max_high, max_low)}.
    Mutates df in place (urgency/queue/method/scores columns) for any row
    in `indices` that still has needs_embedding=True, then clears that flag
    — so re-applying the same or an overlapping filter later is instant for
    rows already refined, and only genuinely new rows in the new slice
    trigger fresh encoding.
    """
    if EMBEDDING_MODEL is None:
        return  # keyword-only result already stored; nothing more to do

    pending_mask = (df.loc[indices, "_needs_embedding"] == True) & \
                   (~df.loc[indices, "_urgency"].isin(["CRITICAL", "HIGH"]))  # noqa: E712
    pending_idx = df.loc[indices][pending_mask].index

    if len(pending_idx) == 0:
        return  # everything in this slice was already refined by an earlier filter

    texts_needed = df.loc[pending_idx, text_col].astype(str)

    # Only encode texts not already in the cache (could've been refined via
    # a different, overlapping filter earlier).
    uncached_texts = sorted(set(t for t in texts_needed if t not in embedding_cache))

    if uncached_texts:
        print(f"  Refining urgency for {len(pending_idx)} rows: "
              f"encoding {len(uncached_texts)} new unique texts "
              f"(cache already has {len(embedding_cache)})")
        vecs = EMBEDDING_MODEL.encode(
            uncached_texts, convert_to_numpy=True, normalize_embeddings=True,
            batch_size=128, show_progress_bar=False,
        )
        high_scores = vecs @ HIGH_EMBEDDINGS.T
        low_scores  = vecs @ LOW_EMBEDDINGS.T
        for i, t in enumerate(uncached_texts):
            embedding_cache[t] = (float(high_scores[i].max()), float(low_scores[i].max()))

    for idx in pending_idx:
        text = df.at[idx, text_col]
        mh, ml = embedding_cache[str(text)]
        kw_level = df.at[idx, "_urgency"]  # provisional keyword-stage level
        if kw_level == "HIGH" or mh >= HIGH_THRESHOLD:
            level, queue = "HIGH", "priority"
        else:
            level, queue = "LOW", "standard"
        df.at[idx, "_urgency"] = level
        df.at[idx, "_queue"] = queue
        df.at[idx, "_method"] = "keyword+embedding"
        df.at[idx, "_score_high"] = round(mh, 3)
        df.at[idx, "_score_low"] = round(ml, 3)
        df.at[idx, "_needs_embedding"] = False


def refine_urgency_for_indices_locked(df: pd.DataFrame, text_col: str, indices, record: dict):
    """Thread-safe wrapper: holds the upload's lock for the duration of
    refinement, so two concurrent /triage requests touching overlapping
    rows can't corrupt the shared dataframe or embedding cache."""
    with record["lock"]:
        refine_urgency_for_indices(df, text_col, indices, record["embedding_cache"])


def triage_urgency_batch(texts: list[str]) -> list[dict]:
    """
    Full batched + deduplicated keyword+embedding triage for /batch-triage
    (pasted-text bulk triage — typically a much smaller list than a file
    upload, so doing the full pipeline immediately is fine here).
    """
    keyword_results = [rule_based_urgency(t) for t in texts]
    needs_embedding_idx = [i for i, r in enumerate(keyword_results) if r["level"] != "CRITICAL"]

    final = [None] * len(texts)
    for i, r in enumerate(keyword_results):
        if r["level"] == "CRITICAL":
            final[i] = {**r, "method": "keyword"}

    if not needs_embedding_idx:
        return final

    if EMBEDDING_MODEL is None:
        for i in needs_embedding_idx:
            final[i] = {**keyword_results[i], "method": "keyword_fallback"}
        return final

    text_to_indices: dict[str, list[int]] = {}
    for i in needs_embedding_idx:
        text_to_indices.setdefault(texts[i], []).append(i)

    unique_texts = list(text_to_indices.keys())
    vecs = EMBEDDING_MODEL.encode(
        unique_texts, convert_to_numpy=True, normalize_embeddings=True,
        batch_size=128, show_progress_bar=False,
    )
    high_scores = vecs @ HIGH_EMBEDDINGS.T
    low_scores  = vecs @ LOW_EMBEDDINGS.T
    max_high = high_scores.max(axis=1)
    max_low  = low_scores.max(axis=1)

    for u_idx, unique_text in enumerate(unique_texts):
        mh = float(max_high[u_idx])
        ml = float(max_low[u_idx])
        for orig_idx in text_to_indices[unique_text]:
            kw = keyword_results[orig_idx]
            if kw["level"] == "HIGH" or mh >= HIGH_THRESHOLD:
                level, queue = "HIGH", "priority"
            else:
                level, queue = "LOW", "standard"
            final[orig_idx] = {
                "level": level, "queue": queue, "method": "keyword+embedding",
                "triggers": kw.get("triggers", []),
                "scores": {"high": round(mh, 3), "low": round(ml, 3)},
            }

    return final


def call_groq(system: str, user: str, max_tokens: int = 700) -> str:
    """Call Groq and return raw text content."""
    resp = client.chat.completions.create(
        model=GROQ_MODEL,
        max_tokens=max_tokens,
        temperature=0.1,
        messages=[
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
    )
    return resp.choices[0].message.content.strip()


def strip_fences(text: str) -> str:
    """Remove markdown code fences the model sometimes adds."""
    text = re.sub(r"^```json\s*", "", text)
    text = re.sub(r"^```\s*",     "", text)
    text = re.sub(r"\s*```$",     "", text)
    return text.strip()


def geo_reopen_rates(substation: str = None,
                      subdivision: str = None,
                      division:    str = None) -> dict:
    """Look up historical reopen rates for the complaint's geography."""
    sub  = (substation  or "").strip().upper()
    subd = (subdivision or "").strip().upper()
    div  = (division    or "").strip().upper()
    return {
        "substation":  REOPEN_BY_SUBSTATION.get(sub,  0.0),
        "subdivision": REOPEN_BY_SUBDIVISION.get(subd, 0.0),
        "division":    REOPEN_BY_DIVISION.get(div,    0.0),
    }

# ---------------------------------------------------------------------------
# File upload — in-memory store + column mapping
#
# Uploaded files are NOT the same as the four backend datasets loaded at
# startup (df1/df2/df3/df_reopen). This is a separate, session-scoped store:
# each upload gets a uuid, the parsed+triaged dataframe is kept in memory
# under that uuid, and later features (complaint-number lookup, substation
# clustering) operate on THIS store, not on the startup datasets.
#
# This is intentionally in-memory only (no DB) for now. Restarting the
# server clears all uploads. Fine for the current "upload a file, analyse
# it" workflow; would need a real store if this needs to persist across
# server restarts or be shared across users.
# ---------------------------------------------------------------------------

UPLOADED_DATASETS: dict = {}   # upload_id -> {"df": DataFrame, "columns": {...}, "filename": str}

# Candidate column names, in priority order, for each logical field.
# Covers both supply_clean.csv and helpdesk_clean.csv naming conventions
# (and is forgiving of close variants in a fresh user-uploaded file).
_COLUMN_CANDIDATES = {
    "complaint_no":  ["COMPLAINT_NO", "APPLICATION_NO", "COMPLAINT_NUMBER", "COMPLAINT NO", "TICKET_NO"],
    "text":          ["REMARKS", "STAFFREMARKS", "COMPLAINT_TEXT", "DESCRIPTION", "COMMENTS"],
    "account_no": [
        "ACCOUNT_NO", "ACCOUNT_NUMBER", "ACC_NO", "CONSUMER_NO",
        "CONSUMER_ACCOUNT_NO", "CONSUMER_NUMBER", "K_NUMBER", "KNO",
    ],
    "substation":    ["SUBSTATION", "supply_substation", "SUB_STATION"],
    "subdivision":   ["SUBDIVISION", "SUB_DIVISION"],
    "division":      ["DIVISION"],
    "zone":          ["ZONE"],
    "circle":        ["CIRCLE"],
    "existing_category": [
        "supply_subtype_std", "canonical_category", "complaint_subtype_norm",
        "COMPLAINT_TYPE",
    ],
    "status":        ["STATUS", "is_closed", "COMPLAINT_STATUS"],
    "officer": [
        "OFFICER", "ASSIGNED_TO", "ASSIGNED_OFFICER", "JE_NAME",
        "ATTENDED_BY", "FIELD_OFFICER", "OFFICER_NAME",
    ],
    "complaint_date": [
        "COMPLAINT_DATE", "REG_DATE", "REGISTRATION_DATE", "COMPLAINT_REG_DATE",
        "CREATED_DATE", "OPEN_DATE", "DATE",
    ],
    "resolved_date": [
        "RESOLVED_DATE", "CLOSED_DATE", "CLOSE_DATE", "RESOLUTION_DATE",
        "RECTIFICATION_DATE", "COMPLETION_DATE",
    ],
}


def _map_columns(df: pd.DataFrame) -> dict:
    """
    Resolve each logical field to whichever actual column name is present
    in the uploaded file. Returns {logical_name: actual_column_or_None}.
    A missing "text" column is the only fatal case (everything else degrades
    gracefully — e.g. no substation column just means no substation grouping).
    """
    mapping = {}
    cols_upper = {c.upper(): c for c in df.columns}
    for logical, candidates in _COLUMN_CANDIDATES.items():
        found = None
        for cand in candidates:
            if cand.upper() in cols_upper:
                found = cols_upper[cand.upper()]
                break
        mapping[logical] = found
    return mapping


def _read_upload(file: UploadFile) -> pd.DataFrame:
    """Read an uploaded CSV or Excel file into a DataFrame."""
    raw = file.file.read()
    name = (file.filename or "").lower()
    try:
        if name.endswith(".xlsx") or name.endswith(".xls"):
            return pd.read_excel(io.BytesIO(raw))
        return pd.read_csv(io.BytesIO(raw))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not parse file: {e}")


# ---------------------------------------------------------------------------
# Downloadable operational report (.docx) — built entirely from data already
# computed for this upload (see _run_upload_analysis). Every section checks
# whether the columns it needs were actually detected in the uploaded file
# and is skipped with a plain-language note if not, rather than guessing or
# inventing numbers. Charts are rendered once, on demand, when the report is
# requested — nothing here runs at upload time, so it doesn't add to the
# time budget for large files.
# ---------------------------------------------------------------------------

_CHART_COLORS = {"CRITICAL": "#b02828", "HIGH": "#8a4f00", "MEDIUM": "#1a63b0", "LOW": "#2e6b0e"}


def _fig_to_stream(fig) -> io.BytesIO:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf


def _add_section_heading(doc: Document, n: int, title: str) -> None:
    doc.add_heading(f"{n}. {title}", level=1)


def _add_note(doc: Document, text: str) -> None:
    p = doc.add_paragraph(text)
    p.runs[0].italic = True
    p.runs[0].font.color.rgb = RGBColor(0x6b, 0x72, 0x80)


def _build_report_docx(record: dict) -> io.BytesIO:
    summary = record["summary"]
    cols = record["columns"]
    doc = Document()

    doc.add_heading("KESCO Complaint Operations Report", level=0)
    meta = doc.add_paragraph()
    meta.add_run(f"Source file: {summary['filename']}   |   Rows analysed: {summary['total_rows']:,}").bold = True
    if summary.get("date_range"):
        doc.add_paragraph(f"Data period: {summary['date_range']['start']} to {summary['date_range']['end']}")
    doc.add_paragraph(
        "This report is generated directly from the uploaded complaint data. "
        "Every figure below is a measured count or rate from this file — nothing "
        "is model-generated. Sections that need a column not present in the "
        "uploaded file are noted and skipped rather than estimated."
    )

    n = 1

    # 1. Urgency breakdown ---------------------------------------------------
    if summary["urgency_breakdown"]:
        _add_section_heading(doc, n, "Urgency Breakdown"); n += 1
        levels = ["CRITICAL", "HIGH", "MEDIUM", "LOW"]
        vals = [summary["urgency_breakdown"].get(l, 0) for l in levels]
        fig, ax = plt.subplots(figsize=(6, 3.3))
        ax.bar(levels, vals, color=[_CHART_COLORS[l] for l in levels])
        ax.set_ylabel("Complaints")
        ax.set_title("Complaints by urgency (keyword/semantic screen)")
        doc.add_picture(_fig_to_stream(fig), width=Inches(6))
        total = sum(vals) or 1
        crit_high_pct = round((vals[0] + vals[1]) / total * 100, 1)
        doc.add_paragraph(f"• {crit_high_pct}% of complaints screened as CRITICAL or HIGH priority ({vals[0] + vals[1]:,} of {total:,}).")

    # 2. Fault category breakdown --------------------------------------------
    _add_section_heading(doc, n, "Fault Category Breakdown"); n += 1
    if summary["fault_category_breakdown"]:
        items = sorted(summary["fault_category_breakdown"].items(), key=lambda x: -x[1])[:15]
        labels, vals = [k for k, _ in items], [v for _, v in items]
        fig, ax = plt.subplots(figsize=(6, max(3, len(labels) * 0.35)))
        ax.barh(labels[::-1], vals[::-1], color="#185FA5")
        ax.set_xlabel("Complaints")
        ax.set_title(f"Top fault categories — source: {summary['fault_category_source']}")
        doc.add_picture(_fig_to_stream(fig), width=Inches(6))
        doc.add_paragraph(
            f"• Most frequent category: '{labels[0]}' with {vals[0]:,} complaints "
            f"({round(vals[0] / summary['total_rows'] * 100, 1)}% of total)."
        )
    else:
        _add_note(doc, summary.get("fault_category_note") or "No fault-category column found in this file.")

    # 3. Substation hotspots ---------------------------------------------------
    _add_section_heading(doc, n, "Substation Hotspots"); n += 1
    if summary["substation_summary"]:
        top = summary["substation_summary"][:15]
        labels = [s["substation"] for s in top]
        crit_high = [s["critical_or_high"] for s in top]
        totals = [s["total_complaints"] for s in top]
        fig, ax = plt.subplots(figsize=(6, max(3, len(labels) * 0.35)))
        y = np.arange(len(labels))
        ax.barh(y, totals[::-1], color="#E6F1FB", label="Total")
        ax.barh(y, crit_high[::-1], color="#A32D2D", label="Critical + High")
        ax.set_yticks(y); ax.set_yticklabels(labels[::-1])
        ax.set_xlabel("Complaints"); ax.legend(loc="lower right", fontsize=8)
        ax.set_title(f"Top {len(labels)} substations by critical + high complaint volume")
        doc.add_picture(_fig_to_stream(fig), width=Inches(6))
        doc.add_paragraph(
            f"• Highest-volume hotspot: '{top[0]['substation']}' with {top[0]['critical_or_high']:,} "
            f"critical/high complaints out of {top[0]['total_complaints']:,} total."
        )
    else:
        _add_note(doc, summary.get("substation_summary_note") or "No substation column found in this file.")

    # 4. Semantic cluster breakdown ---------------------------------------------
    if summary["semantic_cluster_summary"]:
        _add_section_heading(doc, n, "Semantic Cluster Breakdown"); n += 1
        items = summary["semantic_cluster_summary"]
        labels = [c["cluster_name"] for c in items]
        vals = [c["count"] for c in items]
        fig, ax = plt.subplots(figsize=(6, 3.3))
        ax.pie(vals, labels=labels, autopct="%1.0f%%", textprops={"fontsize": 8})
        ax.set_title("Share of complaints by operational cluster")
        doc.add_picture(_fig_to_stream(fig), width=Inches(5.5))

    # 5. Monthly complaint trend --------------------------------------------
    if summary.get("monthly_trend"):
        _add_section_heading(doc, n, "Monthly Complaint Trend"); n += 1
        months = list(summary["monthly_trend"].keys())
        vals = list(summary["monthly_trend"].values())
        fig, ax = plt.subplots(figsize=(6, 3.3))
        ax.plot(months, vals, marker="o", color="#185FA5")
        ax.set_ylabel("Complaints"); ax.set_title("Complaints registered per month")
        plt.setp(ax.get_xticklabels(), rotation=30, ha="right", fontsize=8)
        doc.add_picture(_fig_to_stream(fig), width=Inches(6))
        peak_i = int(np.argmax(vals))
        doc.add_paragraph(f"• Peak month: {months[peak_i]} with {vals[peak_i]:,} complaints.")
    elif cols.get("complaint_date") is None:
        _add_section_heading(doc, n, "Monthly Complaint Trend"); n += 1
        _add_note(doc, "No complaint-date column found in this file, so a monthly trend could not be computed.")

    # 6. Intraday complaint pattern ------------------------------------------
    if summary.get("intraday_pattern"):
        _add_section_heading(doc, n, "Intraday Complaint Pattern"); n += 1
        hours = list(summary["intraday_pattern"].keys())
        vals = list(summary["intraday_pattern"].values())
        fig, ax = plt.subplots(figsize=(6, 2.8))
        ax.bar(hours, vals, color="#8a4f00")
        ax.set_xlabel("Hour of day"); ax.set_ylabel("Complaints")
        ax.set_title("Complaints by hour of registration")
        doc.add_picture(_fig_to_stream(fig), width=Inches(6))
        peak_hr = hours[int(np.argmax(vals))]
        doc.add_paragraph(f"• Peak registration hour: {peak_hr}:00–{peak_hr + 1}:00.")

    # 7. MTTR by category / division -----------------------------------------
    if summary.get("has_mttr") and (summary.get("mttr_by_category") or summary.get("mttr_by_division")):
        _add_section_heading(doc, n, "Mean Time To Resolve (MTTR)"); n += 1
        if summary["mttr_by_category"]:
            items = list(summary["mttr_by_category"].items())[:15]
            labels, vals = [k for k, _ in items], [v for _, v in items]
            fig, ax = plt.subplots(figsize=(6, max(3, len(labels) * 0.35)))
            ax.barh(labels[::-1], vals[::-1], color="#185FA5")
            ax.set_xlabel("Median hours to resolve")
            ax.set_title("MTTR by fault category")
            doc.add_picture(_fig_to_stream(fig), width=Inches(6))
        if summary["mttr_by_division"]:
            items = list(summary["mttr_by_division"].items())[:15]
            labels, vals = [k for k, _ in items], [v for _, v in items]
            fig, ax = plt.subplots(figsize=(6, max(3, len(labels) * 0.35)))
            ax.barh(labels[::-1], vals[::-1], color="#2e6b0e")
            ax.set_xlabel("Median hours to resolve")
            ax.set_title("MTTR by division")
            doc.add_picture(_fig_to_stream(fig), width=Inches(6))
    elif not (cols.get("complaint_date") and cols.get("resolved_date")):
        _add_section_heading(doc, n, "Mean Time To Resolve (MTTR)"); n += 1
        _add_note(doc, "MTTR needs both a complaint date and a resolved/closed date column — one or both were not found in this file.")

    # 8. Pending / aging summary ----------------------------------------------
    if summary.get("pending_summary"):
        _add_section_heading(doc, n, "Pending Complaints & Aging"); n += 1
        ps = summary["pending_summary"]
        fig, ax = plt.subplots(figsize=(4.5, 3))
        ax.bar(["Pending", "Closed"], [ps["pending"], ps["closed"]], color=["#b02828", "#2e6b0e"])
        ax.set_ylabel("Complaints"); ax.set_title("Pending vs. closed")
        doc.add_picture(_fig_to_stream(fig), width=Inches(4.5))
        line = f"• {ps['pending']:,} complaints pending, {ps['closed']:,} closed."
        if ps.get("aging_days"):
            line += f" Pending complaints average {ps['aging_days']['avg']} days old (oldest: {ps['aging_days']['max']} days)."
        doc.add_paragraph(line)

        if summary.get("officer_backlog"):
            doc.add_paragraph("Top officers by pending backlog:").runs[0].bold = True
            table = doc.add_table(rows=1, cols=2)
            table.style = "Light Grid Accent 1"
            hdr = table.rows[0].cells
            hdr[0].text, hdr[1].text = "Officer", "Pending complaints"
            for item in summary["officer_backlog"]:
                row_cells = table.add_row().cells
                row_cells[0].text = str(item["officer"])
                row_cells[1].text = str(item["pending_count"])
    elif cols.get("status") is None:
        _add_section_heading(doc, n, "Pending Complaints & Aging"); n += 1
        _add_note(doc, "No status column found in this file, so pending/closed and aging could not be computed.")

    # 9. Repeat complainants (account-level) ----------------------------------
    if summary.get("repeat_accounts_summary"):
        _add_section_heading(doc, n, "Repeat Complainants (Account-Level)"); n += 1
        rs = summary["repeat_accounts_summary"]
        doc.add_paragraph(
            f"• {rs['accounts_with_multiple_complaints']:,} of {rs['total_unique_accounts']:,} unique "
            f"accounts ({rs['repeat_rate_pct']}%) filed more than one complaint in this period."
        )
        if rs["top_repeat_accounts"]:
            doc.add_paragraph("Top repeat-complaint accounts:").runs[0].bold = True
            table = doc.add_table(rows=1, cols=2)
            table.style = "Light Grid Accent 1"
            hdr = table.rows[0].cells
            hdr[0].text, hdr[1].text = "Account no.", "Complaints filed"
            for item in rs["top_repeat_accounts"]:
                row_cells = table.add_row().cells
                row_cells[0].text = str(item["account_no"])
                row_cells[1].text = str(item["complaint_count"])
        doc.add_paragraph(
            "Use the Account Lookup tab in the app to pull the full complaint "
            "history for any of these accounts."
        )
    elif cols.get("account_no") is None:
        _add_section_heading(doc, n, "Repeat Complainants (Account-Level)"); n += 1
        _add_note(doc, "No consumer account-number column found in this file, so repeat-complainant analysis could not be computed. See the upload template for the expected column name.")

    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return buf


def _build_template_csv() -> io.BytesIO:
    """
    A blank CSV template with the canonical column names the backend knows
    how to detect (see _COLUMN_CANDIDATES), plus two illustrative sample
    rows. Only COMPLAINT_NO and REMARKS are strictly required; every other
    column unlocks one additional report section / filter when present.
    """
    columns = [
        "COMPLAINT_NO", "ACCOUNT_NO", "REMARKS", "COMPLAINT_DATE", "RESOLVED_DATE",
        "STATUS", "SUBSTATION", "SUBDIVISION", "DIVISION", "ZONE", "CIRCLE",
        "COMPLAINT_TYPE", "OFFICER",
    ]
    sample_rows = [
        [
            "KS11042501136", "1023456789", "Transformer mein aag lag gayi hai, poori colony andhere mein hai",
            "2026-04-12 14:30", "2026-04-12 16:10", "CLOSED", "RING ROAD", "RING ROAD SUBDIVISION",
            "DIVISION-3", "ZONE-1", "CIRCLE-2", "DT Damaged/DT Fuse Burnt/Fire on DT", "R. Kashyap",
        ],
        [
            "KS11042501137", "1023456790", "Voltage bahut kam hai, AC aur fridge kharab ho gaye",
            "2026-04-13 09:05", "", "PENDING", "HARRISGANJ", "HARRISGANJ SUBDIVISION",
            "DIVISION-4", "ZONE-1", "CIRCLE-2", "Supply Related (Miscellaneous)", "A. Hussain",
        ],
    ]
    df = pd.DataFrame(sample_rows, columns=columns)
    buf = io.BytesIO()
    df.to_csv(buf, index=False)
    buf.seek(0)
    return buf


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {
        "status": "ok",
        "model":  GROQ_MODEL,
        "embedding_model": "paraphrase-multilingual-MiniLM-L12-v2" if EMBEDDING_MODEL else "unavailable (keyword fallback active)",
        "data": {
            "helpdesk_rows":          len(df1),
            "supply_rows":            len(df2),
            "breakdown_summary_rows": len(df3),
            "reopened_rows":          len(df_reopen),
            "fault_categories":       len(FAULT_CATEGORIES),
            "reopen_substations":     len(REOPEN_BY_SUBSTATION),
            "reopen_subdivisions":    len(REOPEN_BY_SUBDIVISION),
            "reopen_divisions":       len(REOPEN_BY_DIVISION),
            "few_shot_examples":      FEW_SHOT_EXAMPLES.count("\n") + 1 if FEW_SHOT_EXAMPLES else 0,
        },
    }


# ---------------------------------------------------------------------------
@app.post("/analyse")
def analyse_complaint(req: ComplaintRequest):
    """
    Full NLP analysis of a single complaint.
    Two-stage: rule_based_urgency() pre-check, then LLM classification.
    The LLM sees the keyword result as context but can override it.
    """
    pre      = rule_based_urgency(req.text)
    geo      = geo_reopen_rates(req.substation, req.subdivision, req.division)
    cats_str = ", ".join(FAULT_CATEGORIES)

    system_prompt = (
        "You are an NLP engine for a power utility complaint management system "
        "in Kanpur, India (KESCO/DVVNL). You receive raw complaint text in Hindi, "
        "English, or Hinglish and return structured JSON only — "
        "no markdown, no code fences, no preamble, just the JSON object."
    )

    user_prompt = f"""REAL KESCO/DVVNL COMPLAINT EXAMPLES (calibrate your classification on these):
{FEW_SHOT_EXAMPLES or "(loading)"}

COMPLAINT TO CLASSIFY:
Text:        "{req.text}"
Substation:  {req.substation  or "not provided"}
Subdivision: {req.subdivision or "not provided"}
Division:    {req.division    or "not provided"}

GENERAL COMPLAINT REOPEN RATE FOR THIS LOCATION (all complaint types combined,
not specific to this fault category):
  Substation:  {geo["substation"]:.1f}%
  Subdivision: {geo["subdivision"]:.1f}%
  Division:    {geo["division"]:.1f}%

PRE-SCREENED URGENCY (rule engine): {pre["level"]}
RISK TRIGGERS DETECTED: {pre["triggers"]}

Return ONLY this JSON — no other text:
{{
  "urgency": "CRITICAL" or "HIGH" or "MEDIUM" or "LOW",
  "urgency_rationale": "1-2 sentences explaining the urgency level",
  "queue_routing": "bypass_emergency" or "priority_queue" or "standard_queue",
  "semantic_cluster": "Line Faults" or "Transformer Issues" or "Supply Quality" or "Planned Operations" or "Environmental / External",
  "fault_category": "one of: {cats_str}",
  "recommended_action": "specific operational action for the field team",
  "area_reopen_risk": "HIGH" or "MEDIUM" or "LOW" based on the general location reopen rate provided above,
  "extracted_entities": {{
    "location": "location mentioned or null",
    "infrastructure": "affected equipment or null",
    "duration_mentioned": "time duration mentioned or null",
    "affected_services": "hospital/water/school/etc or null"
  }},
  "risk_indicators": ["list", "of", "high-risk", "keywords", "found"],
  "confidence": 0.95
}}"""

    raw = call_groq(system_prompt, user_prompt, max_tokens=700)
    raw = strip_fences(raw)

    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail=f"Model returned non-JSON: {raw}")

    result["rule_based_pre_check"]        = pre
    result["general_reopen_rate_by_geo"]  = geo
    if req.account_no:  result["account_no"]  = req.account_no
    if req.substation:  result["substation"]  = req.substation

    return result


# ---------------------------------------------------------------------------
@app.post("/batch-triage")
def batch_triage(req: BatchRequest):
    """
    Two-stage bulk triage — keyword scan first (CRITICAL tier, near-zero cost),
    then contextual embeddings for anything that passes through (HIGH vs LOW).

    Stage 1 (keyword): CRITICAL complaints are resolved instantly. The keyword
    list covers unambiguous safety signals in Hindi, English, and Hinglish.
    Stage 2 (embedding): non-CRITICAL complaints are embedded and compared
    against HIGH/LOW reference sentences. Catches semantic HIGH cases like
    hospital outages or extended supply failures that keyword lists miss.

    No LLM call — handles hundreds of complaints in seconds.
    Falls back to keyword-only if the embedding model is unavailable.
    Returns list sorted CRITICAL -> HIGH -> MEDIUM -> LOW, with the
    classification method and similarity scores per complaint.
    """
    results = []
    triage_results = triage_urgency_batch(req.complaints)
    for i, (text, result) in enumerate(zip(req.complaints, triage_results)):
        entry = {
            "index":    i,
            "text":     text[:120] + ("\u2026" if len(text) > 120 else ""),
            "urgency":  result["level"],
            "queue":    result["queue"],
            "triggers": result.get("triggers", []),
            "method":   result.get("method", "embedding"),
        }
        # Include similarity scores if available (embedding path only)
        if "scores" in result:
            entry["similarity_scores"] = result["scores"]
        results.append(entry)

    order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    results.sort(key=lambda x: order.get(x["urgency"], 4))
    return {
        "total":               len(results),
        "complaints":          results,
        "classification_method": "keyword+embedding" if EMBEDDING_MODEL else "keyword_fallback",
    }


# ---------------------------------------------------------------------------
# Background processing for large uploads.
#
# Why this exists: a single synchronous request cannot survive the time it
# takes to triage hundreds of thousands of rows, even with batched embedding
# encoding (see triage_urgency_batch docstring). Browsers, dev proxies, and
# most production servers all have request timeouts well under what a
# ~600K-row file needs — the connection drops long before the work finishes,
# even though the backend may still be silently grinding away on it.
#
# So /upload-complaints now does only the FAST part synchronously (parse the
# file, validate it has a usable text column) and returns immediately with a
# upload_id and status="processing". The slow part (triage + clustering +
# aggregation) runs in a background thread. The frontend polls
# /upload-complaints/{upload_id}/status until status="done", then fetches
# /upload-complaints/{upload_id}/summary for the full analysis — the same
# response shape this endpoint used to return directly.
# ---------------------------------------------------------------------------

import threading


def _run_upload_analysis(upload_id: str, df: pd.DataFrame, cols: dict, filename: str):
    """Runs in a background thread. Does the FAST keyword-only pass, then
    stores the finished summary (or an error) back into UPLOADED_DATASETS.

    Deliberately does NOT run embeddings here. Embedding generation is the
    slow part (even batched+deduped, it scales with file size), and most
    of an uploaded file's rows may never actually be inspected by a user —
    they'll typically drill into one substation, one cluster, or one
    complaint type at a time via Urgency Triage filters. So this stage only
    does the keyword scan (CRITICAL rows are fully resolved by this alone),
    and embeddings for everything else are computed lazily, scoped to
    whatever filtered slice a user actually asks for — see
    refine_urgency_for_indices() and the /triage endpoint below.
    """
    try:
        text_col = cols["text"]

        texts = df[text_col].astype(str).tolist()
        scan_results = keyword_scan_batch(texts)

        df["_urgency"]         = [r["level"] for r in scan_results]
        df["_queue"]           = [r["queue"] for r in scan_results]
        df["_triggers"]        = [r.get("triggers", []) for r in scan_results]
        df["_method"]          = [r.get("method", "keyword_only_provisional") for r in scan_results]
        df["_needs_embedding"] = [r.get("needs_embedding", False) for r in scan_results]
        df["_score_high"]      = None
        df["_score_low"]       = None

        if cols["substation"]:
            df["_substation_norm"] = _norm(df[cols["substation"]])
        else:
            df["_substation_norm"] = None

        if cols["complaint_no"]:
            df["_complaint_no_norm"] = _norm(df[cols["complaint_no"]])
        else:
            df["_complaint_no_norm"] = None

        if cols["account_no"]:
            df["_account_no_norm"] = _norm(df[cols["account_no"]])
        else:
            df["_account_no_norm"] = None

        # -- Dates / MTTR (vectorised — safe for month-long, 100K+ row files) --
        if cols["complaint_date"]:
            df["_complaint_dt"] = pd.to_datetime(df[cols["complaint_date"]], errors="coerce", dayfirst=True)
        else:
            df["_complaint_dt"] = pd.NaT

        if cols["resolved_date"]:
            df["_resolved_dt"] = pd.to_datetime(df[cols["resolved_date"]], errors="coerce", dayfirst=True)
        else:
            df["_resolved_dt"] = pd.NaT

        has_date = bool(cols["complaint_date"]) and bool(df["_complaint_dt"].notna().any())
        has_mttr = has_date and bool(cols["resolved_date"]) and bool(df["_resolved_dt"].notna().any())

        if has_mttr:
            mttr_mask = (
                df["_complaint_dt"].notna() & df["_resolved_dt"].notna()
                & (df["_resolved_dt"] >= df["_complaint_dt"])
            )
            df["_mttr_hours"] = np.where(
                mttr_mask,
                (df["_resolved_dt"] - df["_complaint_dt"]).dt.total_seconds() / 3600,
                np.nan,
            )
        else:
            df["_mttr_hours"] = np.nan

        cols_upper = {c.upper(): c for c in df.columns}

        def _first_present(candidates):
            for cand in candidates:
                if cand.upper() in cols_upper:
                    return cols_upper[cand.upper()]
            return None

        complaint_type_col = _first_present(["COMPLAINT_TYPE", "canonical_category"])
        subtype_col        = _first_present(["supply_subtype_std", "complaint_subtype_norm"])
        extra_filter_cols  = {"complaint_type": complaint_type_col, "subtype": subtype_col}

        if cols["existing_category"]:
            df["_cluster"] = df[cols["existing_category"]].apply(_category_to_cluster)
        else:
            df["_cluster"] = "Unclassified"

        urgency_breakdown = df["_urgency"].value_counts().to_dict()

        if cols["existing_category"]:
            fault_category_breakdown = (
                df[cols["existing_category"]].fillna("UNSPECIFIED").value_counts().to_dict()
            )
            fault_category_source = cols["existing_category"]
        else:
            fault_category_breakdown = {}
            fault_category_source = None

        substation_summary = []
        if cols["substation"]:
            for sub, group in df.groupby("_substation_norm"):
                if not sub or sub == "NAN":
                    continue
                sub_urgency = group["_urgency"].value_counts().to_dict()
                substation_summary.append({
                    "substation":          sub,
                    "total_complaints":    len(group),
                    "urgency_breakdown":   sub_urgency,
                    "critical_or_high":    sub_urgency.get("CRITICAL", 0) + sub_urgency.get("HIGH", 0),
                    "general_reopen_rate_pct": REOPEN_BY_SUBSTATION.get(sub, None),
                })
            substation_summary.sort(key=lambda x: -x["critical_or_high"])

        semantic_cluster_summary = []
        total_rows = len(df)
        for cluster_name, group in df.groupby("_cluster"):
            urgency_dist = group["_urgency"].value_counts().to_dict()
            samples = group[text_col].astype(str).head(5).tolist()
            semantic_cluster_summary.append({
                "cluster_name":      cluster_name,
                "count":             len(group),
                "share_pct":         round(len(group) / total_rows * 100, 1),
                "urgency_breakdown": urgency_dist,
                "sample_complaints": samples,
            })
        semantic_cluster_summary.sort(key=lambda x: -x["count"])

        # -- Date range / monthly trend / intraday pattern ---------------------
        date_range = None
        monthly_trend = {}
        intraday_pattern = {}
        if has_date:
            valid_dates = df["_complaint_dt"].dropna()
            date_range = {
                "start": valid_dates.min().strftime("%Y-%m-%d"),
                "end":   valid_dates.max().strftime("%Y-%m-%d"),
            }
            monthly_trend = (
                valid_dates.dt.to_period("M").astype(str).value_counts().sort_index().to_dict()
            )
            hours = valid_dates.dt.hour
            # Only meaningful if the source data actually carries a time-of-day
            # component — a date-only column parses to midnight for every row,
            # which would otherwise render a misleading "all complaints at 00:00" chart.
            if hours.nunique() > 1:
                intraday_pattern = {int(k): int(v) for k, v in hours.value_counts().sort_index().items()}

        # -- MTTR by fault category / division (median hours) ------------------
        mttr_by_category = {}
        mttr_by_division = {}
        if has_mttr:
            mttr_rows = df[df["_mttr_hours"].notna()]
            if cols["existing_category"]:
                mttr_by_category = (
                    mttr_rows.groupby(cols["existing_category"])["_mttr_hours"]
                    .median().round(2).sort_values(ascending=False).to_dict()
                )
            if cols["division"]:
                mttr_by_division = (
                    mttr_rows.groupby(cols["division"])["_mttr_hours"]
                    .median().round(2).sort_values(ascending=False).to_dict()
                )

        # -- Pending / aging summary (requires a status column) ----------------
        pending_summary = None
        if cols["status"]:
            status_norm = df[cols["status"]].astype(str).str.strip().str.upper()
            closed_vals = {"CLOSED", "RESOLVED", "COMPLETED", "DONE", "TRUE", "1", "YES"}
            is_closed = status_norm.isin(closed_vals)
            pending_summary = {
                "pending": int((~is_closed).sum()),
                "closed":  int(is_closed.sum()),
                "aging_days": None,
            }
            if has_date:
                now = pd.Timestamp.now()
                pending_mask = (~is_closed) & df["_complaint_dt"].notna()
                if pending_mask.any():
                    aging = (now - df.loc[pending_mask, "_complaint_dt"]).dt.days
                    pending_summary["aging_days"] = {
                        "avg": round(float(aging.mean()), 1),
                        "max": int(aging.max()),
                    }
            df["_is_closed"] = is_closed
        else:
            df["_is_closed"] = None

        # -- Officer-level pending backlog (requires officer + status) ---------
        officer_backlog = []
        if cols["officer"] and cols["status"]:
            pending_rows = df[~df["_is_closed"].astype(bool)]
            grp = pending_rows.groupby(cols["officer"]).size().sort_values(ascending=False).head(15)
            officer_backlog = [{"officer": k, "pending_count": int(v)} for k, v in grp.items()]

        # -- Repeat complainants / account-level view (requires account_no) ----
        repeat_accounts_summary = None
        if cols["account_no"]:
            acc_counts = df["_account_no_norm"].value_counts()
            acc_counts = acc_counts[(acc_counts.index.notna()) & (acc_counts.index != "NAN") & (acc_counts.index != "")]
            repeat = acc_counts[acc_counts > 1]
            repeat_accounts_summary = {
                "total_unique_accounts": int(acc_counts.shape[0]),
                "accounts_with_multiple_complaints": int(repeat.shape[0]),
                "repeat_rate_pct": round(repeat.shape[0] / acc_counts.shape[0] * 100, 1) if acc_counts.shape[0] else 0.0,
                "top_repeat_accounts": [
                    {"account_no": k, "complaint_count": int(v)} for k, v in repeat.head(15).items()
                ],
            }

        summary = {
            "upload_id":         upload_id,
            "filename":          filename,
            "total_rows":        len(df),
            "columns_detected":  cols,
            "urgency_breakdown": urgency_breakdown,
            "fault_category_breakdown": fault_category_breakdown,
            "fault_category_source": fault_category_source,
            "fault_category_note": (
                None if fault_category_source else
                "No existing category column found in this file, so a fault-category "
                "breakdown could not be computed without per-row LLM classification "
                "(too slow for this file size). Urgency and substation breakdowns are "
                "still fully computed from the keyword+embedding engine."
            ),
            "substation_summary": substation_summary,
            "substation_summary_note": (
                None if cols["substation"] else
                "No substation column found in this file, so substation-wise "
                "patterns could not be computed."
            ),
            "semantic_cluster_summary": semantic_cluster_summary,
            "semantic_cluster_note": (
                None if cols["existing_category"] else
                "No existing category column found in this file, so rows could not "
                "be assigned to a semantic cluster and were grouped as 'Unclassified'. "
                "Call /upload-complaints/{upload_id}/cluster-insight per cluster name "
                "to get an AI-generated insight for any cluster shown here."
            ),
            "classification_method": "keyword_only_provisional (embeddings deferred — see urgency_note)",
            "urgency_note": (
                "Urgency for this upload was computed with a fast keyword-only scan. "
                "CRITICAL is definitive. HIGH/LOW for the rest is provisional and refines "
                "further only where used (e.g. the Live Analyser's per-complaint lookup)."
            ),
            "has_account_no": bool(cols["account_no"]),
            "has_date":       has_date,
            "has_mttr":       has_mttr,
            "date_range":     date_range,
            "monthly_trend":  monthly_trend,
            "intraday_pattern": intraday_pattern,
            "mttr_by_category": mttr_by_category,
            "mttr_by_division": mttr_by_division,
            "pending_summary":  pending_summary,
            "officer_backlog":  officer_backlog,
            "repeat_accounts_summary": repeat_accounts_summary,
        }

        UPLOADED_DATASETS[upload_id].update({
            "df": df,
            "extra_filter_cols": extra_filter_cols,
            "status": "done",
            "summary": summary,
            "error": None,
        })

    except Exception as e:
        UPLOADED_DATASETS[upload_id].update({"status": "error", "error": str(e)})


@app.post("/upload-complaints")
async def upload_complaints(file: UploadFile = File(...)):
    """
    Accepts a CSV/Excel upload, does only the fast validation work
    synchronously, then hands the heavy triage + clustering off to a
    background thread and returns immediately.

    The frontend should poll GET /upload-complaints/{upload_id}/status until
    status == "done" (or "error"), then call
    GET /upload-complaints/{upload_id}/summary for the full analysis.

    This split exists specifically because large files (hundreds of
    thousands of rows) take long enough that no synchronous HTTP request —
    not the browser, not a dev proxy, not most production servers — will
    stay open long enough to return the result directly.
    """
    df = _read_upload(file)
    if df.empty:
        raise HTTPException(status_code=400, detail="Uploaded file has no rows or could not be read.")

    cols = _map_columns(df)
    if cols["text"] is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "Could not find a complaint-text column. Expected one of: "
                f"{_COLUMN_CANDIDATES['text']}. Found columns: {df.columns.tolist()}"
            ),
        )

    text_col = cols["text"]
    df = df[df[text_col].notna() & (df[text_col].astype(str).str.strip() != "")].copy()
    if df.empty:
        raise HTTPException(status_code=400, detail="No rows with non-empty complaint text found.")

    upload_id = str(uuid.uuid4())
    UPLOADED_DATASETS[upload_id] = {
        "df": None,                 # filled in once background processing finishes
        "columns": cols,
        "extra_filter_cols": None,
        "filename": file.filename,
        "status": "processing",
        "summary": None,
        "error": None,
        "embedding_cache": {},      # text -> (max_high, max_low), filled lazily by /triage filters
        "lock": threading.Lock(),   # guards refine_urgency_for_indices against concurrent filter requests
    }

    thread = threading.Thread(
        target=_run_upload_analysis,
        args=(upload_id, df, cols, file.filename),
        daemon=True,
    )
    thread.start()

    return {
        "upload_id":  upload_id,
        "filename":   file.filename,
        "total_rows": len(df),
        "status":     "processing",
    }


@app.get("/upload-complaints/{upload_id}/status")
def upload_status(upload_id: str):
    """Poll this until status is 'done' or 'error'."""
    record = UPLOADED_DATASETS.get(upload_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Unknown upload_id.")
    return {
        "upload_id": upload_id,
        "status":    record["status"],
        "error":     record.get("error"),
    }


@app.get("/upload-complaints/{upload_id}/summary")
def upload_summary(upload_id: str):
    """Full analysis result, available once status == 'done'."""
    record = UPLOADED_DATASETS.get(upload_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Unknown upload_id.")
    if record["status"] == "processing":
        raise HTTPException(status_code=202, detail="Still processing — poll /status until done.")
    if record["status"] == "error":
        raise HTTPException(status_code=500, detail=f"Processing failed: {record.get('error')}")
    return record["summary"]


@app.get("/upload-complaints/{upload_id}/report")
def upload_report(upload_id: str):
    """
    Generates and streams a downloadable .docx operational report built
    from this upload's already-computed summary — no re-processing of the
    underlying rows, so this is fast even for a full month of data. Charts
    are rendered fresh on each call (cheap: aggregate-sized data only).
    """
    record = _get_upload_or_404(upload_id)
    if record["status"] != "done":
        raise HTTPException(status_code=400, detail="Upload is still processing — wait for status == 'done'.")

    buf = _build_report_docx(record)
    filename = f"KESCO_Operations_Report_{record['filename'].rsplit('.', 1)[0]}.docx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/template/download")
def download_template():
    """
    Downloadable CSV template showing every column name the platform can
    detect (see _COLUMN_CANDIDATES). COMPLAINT_NO and REMARKS are the only
    required columns — everything else is optional and simply unlocks one
    more report section (dates -> trends/MTTR, ACCOUNT_NO -> the Account
    Lookup tab and repeat-complainant analysis, STATUS/OFFICER -> pending
    backlog, etc).
    """
    buf = _build_template_csv()
    return StreamingResponse(
        buf,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="KESCO_complaint_upload_template.csv"'},
    )


# ---------------------------------------------------------------------------
@app.get("/upload-complaints/{upload_id}/cluster-insight")
def upload_cluster_insight(upload_id: str, cluster_name: str):
    """
    Lazy, per-cluster AI insight for an already-uploaded file.

    Unlike the original /cluster-insight (which required the frontend to
    supply share_pct and sample_complaints manually), this pulls real
    share_pct and real sample complaint text directly from the stored
    upload — so the insight is generated from this file's actual data, not
    whatever numbers happened to be passed in.

    Intended to be called on-demand (e.g. when a user expands a cluster
    card in the UI), not eagerly for all five clusters at upload time —
    that would mean 5 LLM calls per upload whether or not the user looks
    at them.
    """
    record = UPLOADED_DATASETS.get(upload_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Unknown upload_id. Upload a file via /upload-complaints first.")

    df    = record["df"]
    cols  = record["columns"]
    text_col = cols["text"]

    if "_cluster" not in df.columns:
        raise HTTPException(status_code=400, detail="This upload has no cluster assignment available.")

    group = df[df["_cluster"] == cluster_name]
    if group.empty:
        raise HTTPException(status_code=404, detail=f"No rows found for cluster '{cluster_name}' in this upload.")

    share_pct = round(len(group) / len(df) * 100, 1)
    samples   = group[text_col].astype(str).head(5).tolist()

    insight_req = ClusterInsightRequest(
        cluster_name=cluster_name, share_pct=share_pct, sample_complaints=samples,
    )
    return cluster_insight(insight_req)


# ---------------------------------------------------------------------------
def _get_upload_or_404(upload_id: str) -> dict:
    record = UPLOADED_DATASETS.get(upload_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Unknown upload_id. Upload a file via /upload-complaints first.")
    return record


@app.get("/upload-complaints/{upload_id}/filters")
def upload_filters(upload_id: str):
    """
    Distinct filter values available for this upload, to populate dropdowns:
    substations, complaint types, subtypes/fault types, and semantic clusters.
    Any field the file didn't have a column for comes back as an empty list,
    not an error — the frontend can just hide that dropdown.
    """
    record = _get_upload_or_404(upload_id)
    df, cols, extra = record["df"], record["columns"], record["extra_filter_cols"]

    def _distinct(col):
        if not col or col not in df.columns:
            return []
        vals = df[col].dropna().astype(str).str.strip()
        vals = vals[vals != ""]
        return sorted(vals.unique().tolist())

    return {
        "substations":     sorted(v for v in df["_substation_norm"].dropna().unique().tolist() if v and v != "NAN") if cols["substation"] else [],
        "complaint_types": _distinct(extra["complaint_type"]),
        "subtypes":        _distinct(extra["subtype"]),
        "clusters":        sorted(df["_cluster"].dropna().unique().tolist()),
        "has_complaint_no": bool(cols["complaint_no"]),
    }


@app.get("/upload-complaints/{upload_id}/lookup")
def upload_lookup(upload_id: str, complaint_no: str):
    """
    Look up a complaint by number within a specific upload and run it through
    the same full NLP analysis as /analyse (urgency, fault category,
    recommended action, extracted entities, etc.) — this is what the Live
    Analyser uses when working from an uploaded file's complaint number
    instead of pasted text.

    Complaint numbers are NOT comparable across different uploads/years (see
    module docstring), so this only ever searches within the single upload_id
    given — never across uploads.
    """
    record = _get_upload_or_404(upload_id)
    df, cols = record["df"], record["columns"]

    if not cols["complaint_no"]:
        raise HTTPException(status_code=400, detail="This upload has no complaint-number column.")

    target = complaint_no.strip().upper()
    match = df[df["_complaint_no_norm"] == target]
    if match.empty:
        raise HTTPException(status_code=404, detail=f"Complaint number '{complaint_no}' not found in this upload.")

    row = match.iloc[0]
    req = ComplaintRequest(
        text=str(row[cols["text"]]),
        account_no=str(row[cols["account_no"]])   if cols["account_no"]   and pd.notna(row[cols["account_no"]])   else None,
        substation=str(row[cols["substation"]])   if cols["substation"]   and pd.notna(row[cols["substation"]])   else None,
        subdivision=str(row[cols["subdivision"]]) if cols["subdivision"] and pd.notna(row[cols["subdivision"]]) else None,
        division=str(row[cols["division"]])       if cols["division"]     and pd.notna(row[cols["division"]])     else None,
    )
    result = analyse_complaint(req)
    result["complaint_no"] = str(row[cols["complaint_no"]])
    if cols["existing_category"] and pd.notna(row[cols["existing_category"]]):
        result["existing_category_in_file"] = str(row[cols["existing_category"]])
    return result


# ---------------------------------------------------------------------------
# Account-based segregation — replaces Urgency Triage as the second tab.
#
# Rationale (per KESCO stakeholder feedback): urgency triage isn't useful
# operationally because complaints are handled manually, not routed by an
# automated queue. What supervisors actually want is to pull every complaint
# tied to one consumer account — to see repeat-complaint patterns, spot
# accounts that keep reopening the same issue, and review a consumer's full
# history in one place. Both endpoints below only ever slice the already-
# uploaded, already-keyword-scanned dataframe (groupby / boolean mask), so
# there is no re-processing cost even for a full month of data.
# ---------------------------------------------------------------------------

@app.get("/upload-complaints/{upload_id}/accounts")
def list_accounts(upload_id: str, search: Optional[str] = None, limit: int = 50, offset: int = 0):
    """
    Paginated, most-complaints-first list of consumer accounts in this
    upload. `search` does a substring match on account number so a
    supervisor can jump straight to a specific consumer.
    """
    record = _get_upload_or_404(upload_id)
    df, cols = record["df"], record["columns"]

    if not cols["account_no"]:
        raise HTTPException(status_code=400, detail="This upload has no account-number column.")

    counts = df["_account_no_norm"].value_counts()
    counts = counts[(counts.index.notna()) & (counts.index != "NAN") & (counts.index != "")]

    if search:
        needle = re.escape(search.strip().upper())
        counts = counts[counts.index.str.contains(needle, regex=True)]

    total = int(counts.shape[0])
    page = counts.iloc[offset: offset + limit]

    return {
        "total_accounts": total,
        "accounts": [{"account_no": k, "complaint_count": int(v)} for k, v in page.items()],
        "limit":  limit,
        "offset": offset,
    }


@app.get("/upload-complaints/{upload_id}/account/{account_no}")
def account_detail(upload_id: str, account_no: str):
    """
    Full complaint history for one consumer account within this upload —
    every complaint row tied to that account, sorted chronologically where
    a date column is available, plus urgency/category breakdowns and a
    flag for whether this account is a repeat complainant (>1 complaint).
    """
    record = _get_upload_or_404(upload_id)
    df, cols = record["df"], record["columns"]

    if not cols["account_no"]:
        raise HTTPException(status_code=400, detail="This upload has no account-number column.")

    target = account_no.strip().upper()
    rows = df[df["_account_no_norm"] == target]
    if rows.empty:
        raise HTTPException(status_code=404, detail=f"No complaints found for account '{account_no}' in this upload.")

    text_col = cols["text"]
    if rows["_complaint_dt"].notna().any():
        rows = rows.sort_values("_complaint_dt")

    complaints = []
    for _, row in rows.iterrows():
        complaints.append({
            "complaint_no": str(row[cols["complaint_no"]]) if cols["complaint_no"] and pd.notna(row[cols["complaint_no"]]) else None,
            "text":         str(row[text_col]),
            "date":         row["_complaint_dt"].strftime("%Y-%m-%d") if pd.notna(row["_complaint_dt"]) else None,
            "urgency":      row["_urgency"],
            "substation":   str(row[cols["substation"]])        if cols["substation"]        and pd.notna(row[cols["substation"]])        else None,
            "category":     str(row[cols["existing_category"]]) if cols["existing_category"] and pd.notna(row[cols["existing_category"]]) else None,
            "status":       str(row[cols["status"]])            if cols["status"]            and pd.notna(row[cols["status"]])            else None,
        })

    return {
        "account_no":            account_no,
        "total_complaints":      len(rows),
        "is_repeat_complainant": len(rows) > 1,
        "urgency_breakdown":     rows["_urgency"].value_counts().to_dict(),
        "complaints":            complaints,
    }


@app.get("/upload-complaints/{upload_id}/triage")
def upload_triage(
    upload_id: str,
    substation: Optional[str] = None,
    cluster: Optional[str] = None,
    complaint_type: Optional[str] = None,
    subtype: Optional[str] = None,
):
    """
    Filtered, sorted triage queue over an already-uploaded file. Reuses the
    urgency/queue values computed once at upload time (no re-triage on every
    filter change) and just filters + re-sorts the in-memory rows — this is
    why it's a GET with query params rather than re-posting complaint text.

    Any combination of filters can be applied together (e.g. substation +
    cluster); omitted filters are not applied. Returns the same shape as
    /batch-triage so the frontend can reuse its existing rendering.
    """
    record = _get_upload_or_404(upload_id)
    df, cols, extra = record["df"], record["columns"], record["extra_filter_cols"]

    filtered = df
    applied = {}

    if substation:
        filtered = filtered[filtered["_substation_norm"] == substation.strip().upper()]
        applied["substation"] = substation

    if cluster:
        filtered = filtered[filtered["_cluster"] == cluster]
        applied["cluster"] = cluster

    if complaint_type and extra["complaint_type"]:
        filtered = filtered[
            filtered[extra["complaint_type"]].astype(str).str.strip().str.upper() == complaint_type.strip().upper()
        ]
        applied["complaint_type"] = complaint_type

    if subtype and extra["subtype"]:
        filtered = filtered[
            filtered[extra["subtype"]].astype(str).str.strip().str.upper() == subtype.strip().upper()
        ]
        applied["subtype"] = subtype

    text_col = cols["text"]

    # Lazy embedding step: only NOW, for only THIS filtered slice, do we
    # compute embeddings for any row still marked needs_embedding. This is
    # the whole point of deferring embeddings out of upload time — cost
    # scales with len(filtered), not len(df). Mutates df in place and
    # caches per-text scores on the upload record, so re-applying the same
    # or an overlapping filter later does no repeat encoding work.
    refine_urgency_for_indices_locked(df, text_col, filtered.index, record)

    # Re-slice from df (not the earlier `filtered`) since boolean indexing
    # produced a copy — refine_urgency_for_indices mutated df itself, and
    # we need those updated values, not the pre-refine snapshot.
    filtered = df.loc[filtered.index]

    results = []
    for i, row in filtered.reset_index(drop=True).iterrows():
        results.append({
            "index":       i,
            "text":        str(row[text_col])[:120] + ("\u2026" if len(str(row[text_col])) > 120 else ""),
            "urgency":     row["_urgency"],
            "queue":       row["_queue"],
            "triggers":    row["_triggers"],
            "method":      row["_method"],
            "substation":  row["_substation_norm"] if cols["substation"] else None,
            "cluster":     row["_cluster"],
            "complaint_no": str(row[cols["complaint_no"]]) if cols["complaint_no"] and pd.notna(row[cols["complaint_no"]]) else None,
        })

    order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    results.sort(key=lambda x: order.get(x["urgency"], 4))

    return {
        "total":               len(results),
        "complaints":          results,
        "applied_filters":     applied,
        "classification_method": "keyword+embedding (refined for this filtered slice)" if EMBEDDING_MODEL else "keyword_fallback",
    }


# ---------------------------------------------------------------------------
@app.post("/reopen-risk")
def reopen_risk_analysis(req: ComplaintRequest):
    """
    Classifies a complaint, reports the GENERAL (not fault-specific)
    historical reopen rate for its location, and asks the LLM to suggest
    follow-up checks based on that context.
    """
    classification = analyse_complaint(req)
    geo = geo_reopen_rates(req.substation, req.subdivision, req.division)

    system_prompt = (
        "You are a quality assurance analyst for KESCO/DVVNL Kanpur. "
        "Based on the complaint text and the general reopen rate for its "
        "location, suggest what the field team should double-check before "
        "closing. These are suggestions for a human reviewer to consider, "
        "not verified facts. Return JSON only — no markdown, no preamble."
    )

    user_prompt = f"""COMPLAINT: "{req.text}"
PREDICTED FAULT CATEGORY: {classification.get("fault_category", "unknown")}

GENERAL COMPLAINT REOPEN RATE FOR THIS LOCATION (all complaint types,
not specific to this fault category — no fault-type-level data is available):
  Substation:  {geo["substation"]:.1f}%
  Subdivision: {geo["subdivision"]:.1f}%
  Division:    {geo["division"]:.1f}%

ESCALATION DESKS THAT HISTORICALLY HANDLED REOPENED COMPLAINTS (any type):
{json.dumps(REOPEN_DESK_DIST, indent=2) or "Not available"}

Return ONLY this JSON:
{{
  "area_reopen_probability": "HIGH" or "MEDIUM" or "LOW",
  "likely_missed_root_cause": "your best guess at what the first field team might overlook for this specific complaint",
  "recommended_checks": [
    "suggested check 1",
    "suggested check 2",
    "suggested check 3"
  ],
  "closing_remarks_to_avoid": "a phrase the JE/SDO should avoid using to prematurely close this, in your judgment",
  "escalate_to_senior_engineer": true or false,
  "reopen_prevention_tip": "one suggested action to reduce reopen risk"
}}"""

    raw = call_groq(system_prompt, user_prompt, max_tokens=500)
    raw = strip_fences(raw)

    try:
        risk_result = json.loads(raw)
    except json.JSONDecodeError:
        risk_result = {"area_reopen_probability": "UNKNOWN", "raw": raw}

    return {
        "classification": classification,
        "ai_suggestions": risk_result,
        "ai_suggestions_note": (
            "Everything in ai_suggestions is text generated by the LLM from "
            "the complaint and the general area reopen rate below — it is not "
            "drawn from a database of verified past root causes. Treat it as "
            "a starting point for a human reviewer, not a historical finding."
        ),
        "general_reopen_rate_by_geo": geo,
        "data_scope_note": (
            "Reopen rate is general (all complaint types from this location), "
            "not specific to the predicted fault category. The source reopened "
            "dataset covers billing/service complaints, not breakdown faults, "
            "so a fault-type-specific reopen rate cannot be computed from it."
        ),
    }


# ---------------------------------------------------------------------------
@app.post("/cluster-insight")
def cluster_insight(req: ClusterInsightRequest):
    """
    LLM-generated operational suggestion for a semantic complaint cluster.
    Output is clearly labeled as generated text, not measured historical data.
    """
    samples_str = (
        "\n".join(f"- {s}" for s in req.sample_complaints[:5])
        if req.sample_complaints else "(no samples provided)"
    )

    cluster_cats = SEMANTIC_CLUSTERS.get(req.cluster_name, [])

    system_prompt = (
        "You are an NLP analyst for a power utility in Kanpur, India. "
        "Generate plausible operational suggestions based on the cluster "
        "description and any samples given. These are suggestions for a "
        "human reviewer, not verified findings. Return only a JSON object "
        "— no markdown, no preamble."
    )

    user_prompt = f"""Generate an operational suggestion for this KESCO/DVVNL complaint cluster.

Cluster: "{req.cluster_name}"
Share of total complaints: {req.share_pct:.0f}%
Dataset: KESCO/DVVNL Jan-May 2026
Fault categories in this cluster: {", ".join(cluster_cats)}

Sample complaints:
{samples_str}

Return ONLY this JSON:
{{
  "insight": "3-4 sentences: what complaint text looks like, operational pattern, and one recommendation",
  "peak_risk_times": "your best guess, e.g. summer afternoons May-June, monsoon season July-August",
  "suggested_sop": "one suggested SOP improvement for the operations team"
}}"""

    raw = call_groq(system_prompt, user_prompt, max_tokens=450)
    raw = strip_fences(raw)

    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        result = {"insight": raw}

    return {
        "ai_suggestions": result,
        "ai_suggestions_note": (
            "This is text generated by the LLM from the cluster description "
            "and any sample complaints provided — it is not drawn from "
            "verified historical pattern data. Treat it as a starting point "
            "for a human reviewer, not a measured finding."
        ),
    }


# ---------------------------------------------------------------------------
@app.get("/reopen-patterns")
def reopen_patterns():
    """
    GENERAL complaint reopen analytics by geography. Does NOT include a
    fault-category breakdown — see module docstring for why that join is
    not valid with this data (different complaint domains).
    """
    total_supply   = len(df2)
    total_reopened = len(df_reopen)

    return {
        "by_substation_all":                   REOPEN_BY_SUBSTATION,
        "by_substation_top10_worst":            REOPEN_BY_SUBSTATION_TOP10,
        "by_subdivision":                      REOPEN_BY_SUBDIVISION,
        "by_division":                         REOPEN_BY_DIVISION,
        "reopen_escalation_desk_distribution": REOPEN_DESK_DIST,
        "reopened_complaint_types_seen":       REOPENED_COMPLAINT_TYPES,
        "reopened_complaint_subtypes_seen":    REOPENED_SUBTYPES,
        "reopen_signal_phrases":               REOPEN_SIGNAL_PHRASES,
        "total_supply_complaints":             total_supply,
        "total_reopened_complaints":           total_reopened,
        "overall_reopen_rate_pct": round(
            total_reopened / total_supply * 100, 2
        ) if total_supply > 0 else 0.0,
        "data_scope_note": (
            "These are GENERAL reopen rates by location, covering all complaint "
            "types together. There is no fault-category-level reopen rate: "
            "supply_clean (breakdown/fault complaints) and the reopened dataset "
            "(billing/service/connection complaints) describe different "
            "complaint domains with no shared category vocabulary, so a "
            "type-specific rate cannot be computed from this data."
        ),
    }


# ---------------------------------------------------------------------------
@app.get("/categories")
def get_categories():
    """All static domain constants plus general (geography-based) reopen rates."""
    return {
        "fault_categories":                   FAULT_CATEGORIES,
        "semantic_clusters":                  SEMANTIC_CLUSTERS,
        "critical_keywords":                  CRITICAL_KEYWORDS,
        "high_keywords":                      HIGH_KEYWORDS,
        "general_reopen_rate_by_substation":  REOPEN_BY_SUBSTATION,
        "general_reopen_rate_by_subdivision": REOPEN_BY_SUBDIVISION,
        "general_reopen_rate_by_division":    REOPEN_BY_DIVISION,
        "reopen_signal_phrases":              REOPEN_SIGNAL_PHRASES,
        "model":                              GROQ_MODEL,
        "embedding_model":                    "paraphrase-multilingual-MiniLM-L12-v2" if EMBEDDING_MODEL else "unavailable",
    }


# ---------------------------------------------------------------------------
@app.get("/stats")
def data_stats():
    """
    Summary of all loaded datasets — hit this right after startup to verify
    everything loaded and general (geography-based) reopen rates calculated.
    """
    return {
        "datasets": {
            "helpdesk_clean": {
                "rows":    len(df1),
                "columns": df1.columns.tolist() if not df1.empty else [],
            },
            "supply_clean": {
                "rows":    len(df2),
                "columns": df2.columns.tolist() if not df2.empty else [],
            },
            "breakdown_summary": {
                "rows":    len(df3),
                "columns": df3.columns.tolist() if not df3.empty else [],
            },
            "reopened": {
                "rows":              len(df_reopen),
                "columns":           df_reopen.columns.tolist() if not df_reopen.empty else [],
                "escalation_desks":  REOPEN_DESK_DIST,
                "unique_types_seen": len(REOPENED_COMPLAINT_TYPES),
                "domain_note": (
                    "This file contains billing/service/connection complaints, "
                    "not breakdown/fault complaints."
                ),
            },
        },
        "join_strategy": (
            "Geography only (substation/subdivision/division). "
            "supply_clean=2025, reopened=2026 -- no direct complaint number "
            "overlap, and the two files are different complaint domains, so "
            "no fault-category-level reopen rate is computed."
        ),
        "reopen_rates": {
            "substations_matched":  len(REOPEN_BY_SUBSTATION),
            "subdivisions_matched": len(REOPEN_BY_SUBDIVISION),
            "divisions_matched":    len(REOPEN_BY_DIVISION),
        },
        "nlp_context": {
            "few_shot_examples": FEW_SHOT_EXAMPLES.count("\n") + 1 if FEW_SHOT_EXAMPLES else 0,
            "embedding_model_loaded": EMBEDDING_MODEL is not None,
        },
    }