#!/usr/bin/env bash
# Runs during `docker build` (see Dockerfile): installs dependencies, then
# loads the embedding model once so SentenceTransformer downloads and
# caches its weights under $HF_HOME as part of the image layer, rather
# than on the first request after a cold start / redeploy.
set -euo pipefail

pip install --no-cache-dir -r requirements.txt

python -c "
import os
from sentence_transformers import SentenceTransformer

model_name = os.environ.get('EMBEDDING_MODEL_NAME', 'all-MiniLM-L6-v2')
SentenceTransformer(model_name)
print(f'Cached weights for {model_name} under {os.environ.get(\"HF_HOME\")}')
"
