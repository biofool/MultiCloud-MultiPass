FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py dashboard.py intent.py poller.py registry.py inventory.py paths.py ./
COPY templates/ ./templates/
COPY providers/ ./providers/
COPY cloud_management_client/ ./cloud_management_client/

# Project root for paths.py resolution (auto-detects via pyproject.toml,
# but that isn't copied into the image — set explicitly here).
ENV CLOUDMANAGEMENT_ROOT=/app

EXPOSE 8080

CMD ["python", "main.py"]
