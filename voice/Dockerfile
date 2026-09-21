FROM python:3.12-slim

ARG APP_UID=1000
ARG APP_GID=1000
ARG WHISPER_MODEL=base.en

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    LD_LIBRARY_PATH=/usr/local/lib/python3.12/site-packages/nvidia/cublas/lib:/usr/local/lib/python3.12/site-packages/nvidia/cudnn/lib

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       libportaudio2 \
       libasound2 \
       libasound2-plugins \
    && rm -rf /var/lib/apt/lists/*

# Route ALSA's default device through the host's PipeWire/PulseAudio socket.
# The host sound server owns the hardware and handles sharing and resampling.
RUN printf 'pcm.!default { type pulse }\nctl.!default { type pulse }\n' \
    > /etc/asound.conf

RUN groupadd --gid "${APP_GID}" appgroup \
    && useradd \
       --uid "${APP_UID}" \
       --gid "${APP_GID}" \
       --create-home \
       appuser

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# cpu or cuda. cuda adds the CUDA 12 cuBLAS/cuDNN 9 libraries CTranslate2 needs.
# Declared here so CPU and GPU builds share the apt and pip layers above.
ARG WHISPER_DEVICE=cpu
RUN if [ "${WHISPER_DEVICE}" = "cuda" ]; then \
        pip install --no-cache-dir "nvidia-cublas-cu12" "nvidia-cudnn-cu12>=9,<10"; \
    fi

RUN mkdir -p "/models/${WHISPER_MODEL}" \
    && python -c "from huggingface_hub import snapshot_download; snapshot_download(repo_id='Systran/faster-whisper-${WHISPER_MODEL}', local_dir='/models/${WHISPER_MODEL}')" \
    && chown -R "${APP_UID}:${APP_GID}" /models

# Open-source Piper TTS voice, e.g. en_US-ryan-medium (see rhasspy/piper-voices).
ARG PIPER_VOICE=en_US-ryan-medium
RUN mkdir -p /models/piper \
    && python -c "import sys; from huggingface_hub import hf_hub_download; v=sys.argv[1]; lang_region, name, quality = v.split('-', 2); d=f\"{lang_region.split('_')[0]}/{lang_region}/{name}/{quality}\"; [hf_hub_download('rhasspy/piper-voices', f'{d}/{v}{ext}', local_dir='/tmp/piper') for ext in ('.onnx', '.onnx.json')]" "${PIPER_VOICE}" \
    && find /tmp/piper \( -name "${PIPER_VOICE}.onnx" -o -name "${PIPER_VOICE}.onnx.json" \) -exec mv {} /models/piper/ \; \
    && rm -rf /tmp/piper \
    && chown -R "${APP_UID}:${APP_GID}" /models/piper

COPY voice_client.py segmentation.py .

USER appuser

CMD ["python", "voice_client.py"]
