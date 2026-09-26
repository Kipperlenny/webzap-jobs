# Build context: the repository root (see docker-compose.yml), so the image also contains the job sources.
FROM python:3.12-slim
ARG GIT_COMMIT=dev
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 GIT_COMMIT=$GIT_COMMIT
WORKDIR /app
COPY web/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd -r -u 10001 app && mkdir /data && chown app /data
COPY web/ .
COPY jobagent/ jobagent/
COPY digest.toml external.toml ./
USER app
EXPOSE 8000
HEALTHCHECK CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz')"
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--proxy-headers"]
