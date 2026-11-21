FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv
RUN apt-get update && apt-get install -y --no-install-recommends libseccomp2 \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir fastapi uvicorn pydantic
COPY runner /srv/runner
USER 0:0
EXPOSE 8010
CMD ["sh", "-c", "exec uvicorn runner.app:app --host 0.0.0.0 --port \"${PORT:-8010}\""]
