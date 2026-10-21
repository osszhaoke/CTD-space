FROM docker.io/library/python:3.13-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN python -m venv /app/.venv
COPY collector/requirements.txt ./requirements.txt
RUN /app/.venv/bin/pip install --no-cache-dir -r requirements.txt
COPY db /app/db
COPY collector /app/collector
CMD ["/app/.venv/bin/python", "-m", "collector"]
