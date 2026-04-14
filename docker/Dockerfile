FROM python:3.12-slim
WORKDIR /app

COPY pyproject.toml .
RUN pip install --no-cache-dir .

COPY src/ src/
COPY config/ config/

RUN mkdir -p data

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
  CMD python -c "from pathlib import Path; import sys; \
    h = Path('data/heartbeat.txt'); \
    sys.exit(0 if h.exists() and (h.stat().st_mtime > __import__('time').time() - 600) else 1)"

CMD ["python", "-m", "scanner.run_pipeline", "--force"]
