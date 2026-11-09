FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# PWA icons are binary and travel outside the code snapshot; rebuild them here.
RUN python tools/make_icons.py
# Anti-freeze server settings (QGrab Release.13 pattern):
# worker tmp on /dev/shm + self-recycling workers.
CMD ["gunicorn", "--bind", "0.0.0.0:10000", "--workers", "1", "--threads", "8", \
     "--worker-tmp-dir", "/dev/shm", "--max-requests", "250", \
     "--max-requests-jitter", "50", "app:app"]
