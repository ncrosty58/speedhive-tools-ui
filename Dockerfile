FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
COPY speedhive-tools ./speedhive-tools
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8854

# Single worker so the in-process org-analysis cache (app/analysis_cache.py,
# ~175MB per org) is shared by every request instead of duplicated per worker
CMD ["gunicorn", "--bind", "0.0.0.0:8854", "--workers", "1", "--worker-class", "gthread", "--threads", "8", "--timeout", "180", "--graceful-timeout", "30", "--access-logfile", "-", "--error-logfile", "-", "app:app"]
