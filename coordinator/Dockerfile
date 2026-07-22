# Build context is the repo root (see docker-compose.yml), so this
# Dockerfile can reach both coordinator/ and the shared protocol/ dir.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1
WORKDIR /app

COPY coordinator/requirements.txt ./coordinator/requirements.txt
RUN uv pip install --system --no-cache -r coordinator/requirements.txt

COPY coordinator/app ./coordinator/app

WORKDIR /app/coordinator

HEALTHCHECK --interval=5s --timeout=3s --retries=5 CMD python -c "\
import ssl, urllib.request; \
ctx = ssl.create_default_context(); \
ctx.check_hostname = False; \
ctx.verify_mode = ssl.CERT_NONE; \
urllib.request.urlopen('https://localhost:8443/health', context=ctx, timeout=3)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8443", \
     "--ssl-certfile", "/certs/coordinator.crt", "--ssl-keyfile", "/certs/coordinator.key"]
