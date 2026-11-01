FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    HF_HOME=/models FASTEMBED_CACHE_PATH=/models
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg curl && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
# Pre-descarga del modelo de embeddings (multilingüe, ~120 MB) para arranque rápido
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2')" || true

COPY . .
EXPOSE 8000
CMD ["uvicorn", "senda.web.app:app", "--host", "0.0.0.0", "--port", "8000"]
