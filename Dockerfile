FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir -e .
ENV MERIDIAN_STATE_DB=/app/data/meridian-state.db
EXPOSE 8000
CMD ["sh", "-c", "meridian web --host 0.0.0.0 --port ${PORT:-8000}"]
