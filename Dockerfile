FROM mcr.microsoft.com/playwright/python:v1.60.0-noble

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY monitor.py .
RUN mkdir -p /data && chown -R pwuser:pwuser /app /data
USER pwuser

EXPOSE 8080

CMD ["python", "monitor.py"]
