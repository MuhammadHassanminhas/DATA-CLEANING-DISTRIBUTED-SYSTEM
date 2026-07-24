FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1
WORKDIR /app

COPY dashboard/requirements.txt ./dashboard/requirements.txt
RUN uv pip install --system --no-cache -r dashboard/requirements.txt

COPY dashboard/app ./dashboard/app

WORKDIR /app/dashboard

HEALTHCHECK --interval=5s --timeout=3s --retries=5 CMD python -c "\
import ssl, urllib.request; \
ctx = ssl.create_default_context(); \
ctx.check_hostname = False; \
ctx.verify_mode = ssl.CERT_NONE; \
urllib.request.urlopen('https://localhost:8444/health', context=ctx, timeout=3)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8444", \
     "--ssl-certfile", "/certs/dashboard.crt", "--ssl-keyfile", "/certs/dashboard.key"]
