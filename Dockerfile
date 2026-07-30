# Stage 5: production image
# TODO: install requirements.txt, pre-download all-MiniLM-L6-v2 weights at
# build time (see build.sh), run `uvicorn app.app:app` as CMD.
FROM python:3.11-slim
