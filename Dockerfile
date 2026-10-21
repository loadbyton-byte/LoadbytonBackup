FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY leadengine ./leadengine
COPY dashboard ./dashboard
COPY site ./site
# Run unprivileged; /data is the persistent volume holding the SQLite databases (single-tenant file, or the control DB + one file per workspace).
RUN useradd --system --uid 10001 app && mkdir /data && chown app /data
USER app
ENV LEADENGINE_DB=/data/leadengine.db DATA_DIR=/data
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD python -m leadengine.healthcheck
# Platform mode when CONTROL_DB is set (accounts, workspaces, site); otherwise the single-tenant app. Demo: `python -m leadengine.cli serve --demo --host 0.0.0.0`.
CMD ["uvicorn", "leadengine.main:create", "--factory", "--host", "0.0.0.0", "--port", "8000"]
