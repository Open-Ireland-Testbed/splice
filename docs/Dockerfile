FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src src

ENV PYTHONPATH=src

ENTRYPOINT ["python", "src/splice/cli.py"]
