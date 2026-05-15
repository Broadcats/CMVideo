FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/app/.cache/huggingface \
    XDG_CACHE_HOME=/app/.cache \
    CMVIDEO_WORDLISTS_DIR=/app/wordlists

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 user
WORKDIR /app

COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Pre-fetch whisper-tiny.en so the first request doesn't pay the
# ~75 MB model download tax. Bakes the model into the image at build
# time and re-uses HF_HOME for runtime cache.
RUN python - <<'PY'
from faster_whisper import WhisperModel
WhisperModel("tiny.en", device="cpu", compute_type="int8")
print("whisper tiny.en cached")
PY

COPY app.py mini_censor.py /app/
COPY wordlists /app/wordlists
COPY templates /app/templates
COPY static /app/static

RUN chown -R user:user /app
USER user

EXPOSE 7860
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "7860"]
