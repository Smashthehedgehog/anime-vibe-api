# Production image for the FastAPI + MCP server (app/app.py).
#
# The one thing worth calling out: build.sh runs `pip install` and then
# loads the fastembed model once, *during the build*, so the
# all-MiniLM-L6-v2 ONNX weights get baked into this image layer instead
# of being downloaded from Hugging Face on every cold start. HF_HOME
# below pins where that cache lives so the build step and the running
# container agree on the path (fastembed doesn't read HF_HOME on its
# own -- app.py and vector_worker.py both pass it explicitly as
# TextEmbedding's `cache_dir`).
#
# Not used for the cron job (see render.yaml's `dockerCommand`) -- that
# reuses this same image but runs ingestion_worker.py /
# vector_worker.py instead of starting uvicorn, so the cached model
# weights get reused there too instead of re-downloading per run.

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/opt/hf-cache \
    EMBEDDING_MODEL_NAME=all-MiniLM-L6-v2

WORKDIR /app

COPY requirements.txt build.sh ./
RUN chmod +x build.sh && ./build.sh

# huggingface_hub (a fastembed dependency) still does live ETag-
# revalidation HEAD requests against huggingface.co on every load *even
# when the files are already cached*, unless told not to. Left unset,
# that reintroduces exactly the network round-trip (and dependency on HF
# being reachable) that baking the weights in at build time was supposed
# to eliminate. Set only now, after build.sh's initial download needed
# real network access -- this only affects the runtime container.
ENV HF_HUB_OFFLINE=1

COPY . .

# Informational -- Render (see render.yaml) supplies the real port to
# bind via $PORT at runtime, read below in the shell-form CMD.
EXPOSE 8000

CMD ["sh", "-c", "uvicorn app.app:app --host 0.0.0.0 --port ${PORT:-8000}"]
