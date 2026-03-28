ARG BASE_IMAGE=docker.1ms.run/library/python:3.12-slim
FROM ${BASE_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app /app/app
COPY static /app/static
COPY server.py /app/server.py
COPY README.md /app/README.md
COPY auth.example.json /app/auth.example.json

RUN mkdir -p /app/data

EXPOSE 14785

CMD ["python3", "server.py"]
