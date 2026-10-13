# Build context is rootfs/ (see build.json). FROM is arch-agnostic.
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY . /app

EXPOSE 8080
CMD ["python", "app.py"]
