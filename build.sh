#!/usr/bin/env bash
# Runs during `docker build` (see Dockerfile): installs dependencies, then
# loads the embedding model once so fastembed downloads and caches its
# ONNX weights under $HF_HOME as part of the image layer, rather than on
# the first request after a cold start / redeploy.
set -euo pipefail

pip install --no-cache-dir -r requirements.txt

python -c "
import os
from fastembed import TextEmbedding

model_name = os.environ.get('EMBEDDING_MODEL_NAME', 'all-MiniLM-L6-v2')
fastembed_name = model_name if '/' in model_name else f'sentence-transformers/{model_name}'
TextEmbedding(model_name=fastembed_name, cache_dir=os.environ.get('HF_HOME'))
print(f'Cached weights for {fastembed_name} under {os.environ.get(\"HF_HOME\")}')
"
