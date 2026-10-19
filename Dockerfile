FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY leadengine ./leadengine
COPY dashboard ./dashboard
ENV LEADENGINE_DB=/data/leadengine.db
VOLUME /data
EXPOSE 8000
CMD ["uvicorn", "leadengine.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
