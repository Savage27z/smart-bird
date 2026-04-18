FROM python:3.11-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Create unprivileged user and writable data dir.
RUN useradd --system --create-home --uid 1000 smartbird \
    && mkdir -p /data \
    && chown -R smartbird:smartbird /data /app

COPY --chown=smartbird:smartbird . .

USER smartbird
VOLUME ["/data"]
STOPSIGNAL SIGTERM
CMD ["python", "-u", "main.py"]
