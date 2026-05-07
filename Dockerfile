# Dockerfile (for Cloud Run)
FROM python:3.13-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# 1) Install minimal runtime libs your Python packages may need
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      curl \
      wget \
      ca-certificates \
      gnupg \
    && rm -rf /var/lib/apt/lists/*

# 2) Copy requirements and install Python packages
COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade pip
RUN pip install --no-cache-dir -r /app/requirements.txt

# 3) Copy app code
COPY . /app

# Environment variable (Cloud Run injects $PORT automatically)
ENV PORT=8080

# Expose port (Cloud Run listens here)
EXPOSE 8080

# 4) Use gunicorn to run Flask app
# Replace `app:app` with `filename:flask_app_variable` if your file/variable are different
CMD exec gunicorn --bind :$PORT --workers 2 --threads 8 --timeout 0 app:app
