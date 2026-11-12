# syntax=docker/dockerfile:1
FROM python:3.11-slim

# Install system dependencies for PDF generation, Barcodes, Fonts, and SQLite
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libgl1 \
    libglib2.0-0 \
    fonts-dejavu-core \
    fonts-liberation \
    curl \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy and install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY . .

# Ensure data and uploads directories exist with write permissions
RUN mkdir -p /app/data /app/static/uploads && chmod -R 777 /app/data /app/static/uploads

ENV PYTHONUNBUFFERED=1 \
    PORT=5000 \
    DATABASE_PATH=/app/data/katalog.db

EXPOSE 5000

COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh
ENTRYPOINT ["docker-entrypoint.sh"]

CMD ["gunicorn", "--workers", "3", "--threads", "2", "--bind", "0.0.0.0:5000", "--timeout", "120", "app:app"]
