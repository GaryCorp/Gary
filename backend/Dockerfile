# syntax=docker/dockerfile:1
FROM python:3.12-slim

ARG APP_UID=1000
ARG APP_GID=1000

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN groupadd --gid "${APP_GID}" appgroup \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" --create-home appuser

WORKDIR /app

COPY requirements.txt .
# uv resolves and installs much faster than pip for CrewAI's large dependency
# tree; the cache mount keeps downloads between builds without adding them to
# the image.
RUN --mount=type=cache,target=/root/.cache/uv \
    pip install --no-cache-dir uv==0.12.15 \
    && uv pip install --system -r requirements.txt

COPY gary ./gary
COPY app ./app

USER appuser

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
