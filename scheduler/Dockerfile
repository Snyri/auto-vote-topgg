FROM python:3.13-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN pip install --no-cache-dir --no-deps \
        certifi==2026.7.22 \
        charset-normalizer==3.4.9 \
        idna==3.18 \
        urllib3==2.7.0 \
        requests==2.34.2 \
    && python -m pip check \
    && addgroup -S app \
    && adduser -S -G app app

WORKDIR /app

COPY --chown=app:app scheduler.py /app/scheduler.py

USER app

CMD ["python", "-u", "/app/scheduler.py"]
