FROM node:24-bookworm-slim AS frontend
WORKDIR /web
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HSR_DATA_DIR=/data HSR_MODE=server
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --uid 10001 --create-home story \
    && mkdir /data && chown story:story /data
COPY backend/ ./backend/
COPY --from=frontend /web/dist/ ./frontend/dist/
USER story
EXPOSE 8765
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/v1/health',timeout=3)"
CMD ["python", "-m", "backend.app", "--host", "0.0.0.0", "--port", "8765", "--mode", "server", "--data-dir", "/data"]
