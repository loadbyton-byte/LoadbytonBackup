FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY leadengine ./leadengine
COPY dashboard ./dashboard
# Run unprivileged; /data is the persistent volume holding the SQLite database.
RUN useradd --system --uid 10001 app && mkdir /data && chown app /data
USER app
ENV LEADENGINE_DB=/data/leadengine.db
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"
# Production: uvicorn factory. Demo: override with `python -m leadengine.cli serve --demo --host 0.0.0.0`.
CMD ["uvicorn", "leadengine.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
