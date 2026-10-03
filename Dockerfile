FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy every root-level module — the explicit per-file list this replaces
# silently dropped 11 modules main.py imports transitively (alerts, dedup,
# killswitch_actions, intent_*, admin_routes, secret_loader, ...), which
# broke cold start on `import main` (#3).
COPY *.py ./
COPY templates/ ./templates/
COPY providers/ ./providers/
COPY cloud_management_client/ ./cloud_management_client/

# Project root for paths.py resolution (auto-detects via pyproject.toml,
# but that isn't copied into the image — set explicitly here).
ENV CLOUDMANAGEMENT_ROOT=/app

EXPOSE 8080

CMD ["python", "main.py"]
