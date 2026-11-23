FROM python:3.12-slim
LABEL org.opencontainers.image.source=https://github.com/poirpomcirculab/slskd-gluetun-store
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir mutagen==1.47.0 tzdata
COPY trieur.py /app/trieur.py

CMD ["python", "-u", "/app/trieur.py"]
