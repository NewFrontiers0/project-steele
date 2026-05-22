FROM python:3.12-slim

# iputils-ping for reload detection and latency probes.
# iproute2 for local gateway detection in the latency tool.
# ca-certificates because the Meraki SDK and firmware downloads need them.
RUN apt-get update && apt-get install -y --no-install-recommends \
        iputils-ping \
        iproute2 \
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

# Firmware and local user profiles live in volumes so they survive restarts.
RUN mkdir -p /app/firmware /app/data
VOLUME ["/app/firmware", "/app/data"]

ENV CLI_MAX_PARALLEL=10
ENV LATENCY_MAX_PARALLEL=12
ENV PROJECT_STEELE_USERS_FILE=/app/data/users.json

EXPOSE 8001 9000 2121 69/udp 30000 30001 30002 30003 30004 30005 30006 30007 30008 30009

ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001"]
