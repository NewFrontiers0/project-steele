FROM python:3.12-slim

# iputils-ping for the reload-detection probe.
# ca-certificates because the Meraki SDK and firmware downloads need them.
RUN apt-get update && apt-get install -y --no-install-recommends \
        iputils-ping \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first in their own layer so code changes don't trigger
# a full pip reinstall on rebuild.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the app. .dockerignore keeps .venv, __pycache__, and firmware out.
COPY *.py ./
COPY static/ ./static/
COPY docker-entrypoint.sh ./
RUN chmod +x docker-entrypoint.sh

# Firmware lives in a volume so re-downloads are skipped across restarts.
RUN mkdir -p /app/firmware
VOLUME ["/app/firmware"]

EXPOSE 8001 9000 2121 69/udp 30000 30001 30002 30003 30004 30005 30006 30007 30008 30009

ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001"]
