FROM python:3.13-alpine

RUN pip install --no-cache-dir requests==2.34.2

WORKDIR /app

COPY scheduler.py /app/scheduler.py

CMD ["python", "-u", "/app/scheduler.py"]
