FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv
RUN apt-get update && apt-get install -y --no-install-recommends libseccomp2 \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir fastapi uvicorn pydantic
COPY runner /srv/runner
USER 0:0
EXPOSE 8010
CMD ["uvicorn", "runner.app:app", "--host", "0.0.0.0", "--port", "8010"]
