# Risk Radar backend image: one image, three roles (api, worker, clocks),
# chosen by the command. The model artefacts in ml/artifacts ship inside it,
# content-hashed and checked against the registry at load (D15a).
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    RISKRADAR_ENV=production \
    RISKRADAR_API_HOST=0.0.0.0 \
    RISKRADAR_API_PORT=8000

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ backend/
COPY scripts/ scripts/
COPY simulator/ simulator/
COPY fixtures/ fixtures/
COPY ml/artifacts/*.joblib ml/artifacts/
COPY ml/artifacts/evaluation-*.json ml/artifacts/
COPY ml/*.py ml/

RUN useradd --create-home --uid 10001 riskradar && chown -R riskradar /app
USER riskradar

EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/live').status == 200 else 1)"

CMD ["python", "scripts/run_api.py"]
