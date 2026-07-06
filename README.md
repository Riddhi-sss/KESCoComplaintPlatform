# NLP Complaint Analysis Platform
## KESCO / DVVNL · Phase 4
 
> Semantic clustering and urgency triage for power utility complaints.
 
### Quick start
 
```bash
# 1. Backend
cd backend
python -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
export GROQ_API_KEY="gsk_your_groq_api_key_here..."
uvicorn main:app --reload --port 8000
 
# 2. Frontend (new terminal)
cd frontend
npm install
npm run dev
# → Open http://localhost:5173
```
 
See `docs/IMPLEMENTATION_GUIDE.md` for the full setup, API reference,
data integration examples, and production deployment steps.
 

