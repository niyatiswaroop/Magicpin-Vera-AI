# Host-agnostic image: works on Hugging Face Spaces, Render, Fly, Koyeb, Cloud Run.
FROM python:3.12-slim

WORKDIR /app

# Dependencies first so the layer caches across code edits.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Only what the server needs at runtime. The dataset is deliberately excluded --
# the judge pushes every context over /v1/context, so shipping it would add
# weight and risk the bot answering from stale local copies instead.
COPY bot.py composer.py conversation_handlers.py facts.py kinds.py voice.py \
     llm.py store.py ./

# Hugging Face Spaces routes to 7860; every other host injects its own $PORT.
ENV PORT=7860 \
    PYTHONUNBUFFERED=1 \
    VERA_DB=/tmp/vera_state.db \
    VERA_USE_LLM=0
EXPOSE 7860

# The judge polls /healthz every 60s and disqualifies after 3 consecutive
# failures, so the container reports its own liveness too.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,os,sys; \
      sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",7860)}/v1/healthz', timeout=4).status==200 else 1)"

# One worker on purpose: context lives in this process, and a second worker would
# serve requests from an empty store, failing the 255-context warmup gate (§4).
CMD ["sh", "-c", "uvicorn bot:app --host 0.0.0.0 --port ${PORT:-7860} --workers 1 --log-level warning"]
