# Hugging Face Spaces (and most container hosts) expect the app to listen
# on the port given by $PORT — Spaces sets this to 7860 automatically.

FROM python:3.11-slim

# Spaces builds/runs containers as a non-root user (uid 1000) — creating it
# explicitly avoids permission errors writing to the model cache directory.
RUN useradd -m -u 1000 appuser
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bake the sentence-transformers model into the image at build time rather
# than downloading it on every cold start — Spaces' disk is not persistent,
# so without this every restart would re-download ~470MB from huggingface.co
# before the app could serve a single request.
RUN python -c "from sentence_transformers import SentenceTransformer; \
    SentenceTransformer('paraphrase-multilingual-MiniLM-L12-v2')"

COPY . .
RUN chown -R appuser:appuser /app
USER appuser

ENV PORT=7860
EXPOSE 7860

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT}"]