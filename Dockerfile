FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8854

CMD ["gunicorn", "--bind", "0.0.0.0:8854", "--workers", "3", "--worker-class", "gthread", "--threads", "4", "--timeout", "180", "--graceful-timeout", "30", "--access-logfile", "-", "--error-logfile", "-", "app:app"]
