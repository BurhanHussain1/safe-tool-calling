# Single image, three roles. The mock API, the assistant and the UI share the
# same package, so building once and varying the command keeps them provably in
# sync — a schema change cannot reach one service and not another.
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first, so editing source does not invalidate the install layer.
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-deps -e .

# Non-root: the container has no business writing to its own source tree.
RUN useradd --create-home --uid 10001 nl2api && chown -R nl2api:nl2api /app
USER nl2api

EXPOSE 8000 8001 8501

# Overridden per service in docker-compose.yml.
CMD ["uvicorn", "nl2api.mock_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
