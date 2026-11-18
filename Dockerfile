FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend ./backend
COPY alembic ./alembic
COPY alembic.ini .
COPY web ./web
RUN useradd --system --uid 10001 --create-home kite && mkdir -p /data && chown kite:kite /data
USER 10001:10001
EXPOSE 8000
CMD ["sh", "-c", "exec uvicorn backend.app:app --host 0.0.0.0 --port \"${PORT:-8000}\""]
