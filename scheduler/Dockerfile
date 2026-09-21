FROM python:3.13-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN pip install --no-cache-dir requests==2.34.2 \
    && addgroup -S app \
    && adduser -S -G app app

WORKDIR /app

COPY --chown=app:app scheduler.py /app/scheduler.py

USER app

CMD ["python", "-u", "/app/scheduler.py"]
