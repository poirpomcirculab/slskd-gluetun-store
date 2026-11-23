FROM python:3.12-slim
LABEL org.opencontainers.image.source=https://github.com/poirpomcirculab/slskd-gluetun-store
RUN pip install --no-cache-dir bandcampsync==0.9.0 tzdata
COPY synchro.py /app/synchro.py
CMD ["python", "-u", "/app/synchro.py"]
