FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends libsndfile1 ffmpeg && \
    rm -rf /var/lib/apt/lists/*

# faster-whisper uses ctranslate2 which dlopens libcublas/libcudnn at CUDA
# init. The pip wheels ship them under site-packages/nvidia/*/lib; the
# preload function in server.py loads them with RTLD_GLOBAL at runtime.
RUN pip install --no-cache-dir \
    "faster-whisper>=1.1.0" \
    "nvidia-cublas-cu12" \
    "nvidia-cudnn-cu12" \
    fastapi \
    "uvicorn[standard]" \
    soundfile \
    numpy \
    python-multipart \
    httpx \
    loguru

WORKDIR /app
COPY server.py /app/server.py

EXPOSE 8000
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
