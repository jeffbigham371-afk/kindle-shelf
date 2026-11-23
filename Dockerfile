FROM python:3.12-slim-bookworm

# Calibre provides ebook-convert / ebook-meta, which do the actual conversion.
RUN apt-get update \
 && apt-get install -y --no-install-recommends calibre fonts-dejavu-core ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

ENV DATA_DIR=/data \
    QT_QPA_PLATFORM=offscreen \
    PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
