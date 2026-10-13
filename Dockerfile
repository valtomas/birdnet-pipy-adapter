# The Supervisor app build uses the add-on repository root as the Docker build
# context, so app code and requirements live under rootfs/.
FROM python:3.12-slim

WORKDIR /app
COPY rootfs/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY rootfs/ /app/

EXPOSE 8080
CMD ["python", "app.py"]
