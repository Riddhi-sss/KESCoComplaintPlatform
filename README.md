# ⚡ KESCO Complaint Analysis Platform

An NLP-powered complaint triage and analysis platform built during an internship with **KESCO (Kanpur Electricity Supply Company)**. It turns raw, messy consumer complaint exports — written in a mix of Hindi, English, and Hinglish — into structured operational insight: urgency classification, fault categorization, substation hotspots, repeat-complainant tracking, and a downloadable operations report, all generated automatically from whatever file a supervisor uploads.

**🔗 Live demo:** [complaint-platform-yourname.vercel.app](#) <!-- replace with your actual Vercel URL -->
**⚙️ Backend API:** [hridhimasrivastava-kescocomplaintplatform.hf.space/docs](https://hridhimasrivastava-kescocomplaintplatform.hf.space/docs)

> **Note:** the backend runs on Hugging Face's free tier, which sleeps after 48 hours of inactivity. If the demo seems slow on first load, give it 30–60 seconds to wake up — that's the backend restarting, not a bug.

---

## What it does

A field supervisor or analyst uploads a CSV/XLSX export of consumer complaints — no fixed schema required, the platform auto-detects whichever columns are present — and gets back:

- **Urgency triage** — every complaint classified CRITICAL / HIGH / MEDIUM / LOW using a fast keyword screen, escalating to semantic (embedding-based) and LLM classification where it matters
- **Fault category & substation breakdowns** — where the pain points are concentrated
- **Semantic clustering** — complaints grouped into operational buckets by meaning, not just keyword
- **Time-based analysis** — monthly trend, intraday complaint pattern, and Mean Time To Resolve (MTTR) by category/division, wherever the file has date columns
- **Pending & aging tracking** — open vs. closed complaints, officer-level backlog, wherever the file has a status column
- **Repeat-complainant detection** — which consumer accounts keep filing complaints, wherever the file has an account number column
- **A downloadable Word report** — every chart and finding above, packaged for sharing with people who'll never open the app itself
- **Account-level lookup** — pull one consumer's full complaint history in one click
- **Single-complaint live analysis** — look up any complaint by number for an instant full NLP breakdown (urgency rationale, recommended action, reopen risk)

Every section of the report and every feature gracefully degrades if a column isn't present in the uploaded file — nothing crashes, nothing is invented; the platform just tells you what it couldn't compute and why.

## Try it yourself

A synthetic dataset (2,500 realistic complaints, in the format the platform expects) is included at [`demo-data/KESCO_synthetic_complaints_June2026.csv`](./demo-data/KESCO_synthetic_complaints_June2026.csv) — no real consumer data involved. Download it, upload it through the "Upload & analyse" tab on the live demo, and every feature above will populate.

## Architecture

```
┌─────────────────────┐         ┌──────────────────────────────────┐
│   React + Vite       │  HTTP   │   FastAPI backend                 │
│   (Vercel)           │────────▶│   (Docker on Hugging Face Spaces) │
│                       │         │                                    │
│  • Upload & analyse   │         │  • pandas — vectorized aggregation│
│  • Account lookup     │         │  • sentence-transformers          │
│  • Live analyser       │         │    (multilingual MiniLM embeddings)│
└─────────────────────┘         │  • Groq (Llama 3.3 70B) — LLM     │
                                  │    classification & insights       │
                                  │  • matplotlib + python-docx —      │
                                  │    generated Word reports          │
                                  └──────────────────────────────────┘
```

Large files (a full month of complaint data) are processed in a background thread — the upload endpoint returns immediately with an `upload_id`, and the frontend polls for completion, so the UI never blocks on a slow analysis job.

## Tech stack

**Backend:** FastAPI · pandas · sentence-transformers · Groq (Llama 3.3 70B) · matplotlib · python-docx · uvicorn

**Frontend:** React · Vite · Recharts

**Deployment:** Docker → Hugging Face Spaces (backend) · Vercel (frontend)

## Running it locally

**Backend:**
```bash
cd backend
python -m venv venv
venv\Scripts\activate        # Windows; use `source venv/bin/activate` on Mac/Linux
pip install -r requirements.txt
# create a .env file with: GROQ_API_KEY=your_key_here
uvicorn main:app --reload
```

**Frontend:**
```bash
cd frontend
npm install
npm run dev
```

By default the frontend talks to `localhost:8000` via Vite's dev proxy. To point it at a deployed backend instead, set `VITE_API_BASE` in a `frontend/.env` file (see `.env.example`).

## Known limitations

- **In-memory storage** — uploaded file analyses live in server RAM for the life of the process; they don't survive a backend restart. Fine for demo/single-session use; would need a database layer for long-term persistence.
- **Startup reference data** — a few small features (general reopen-rate baselines, the dashboard's top summary cards) are powered by historical KESCO datasets that are intentionally excluded from this public repository for consumer privacy. These features show as empty in the public deployment; everything upload-based is fully functional regardless.

## Author

Built by Hridhima Srivastava — [GitHub](https://github.com/Riddhi-sss)