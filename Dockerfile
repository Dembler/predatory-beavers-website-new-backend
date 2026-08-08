FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN python -m pip install uv==0.12.1 \
    && python -m uv sync --frozen --no-dev --no-editable

ENV PATH="/app/.venv/bin:$PATH"

COPY alembic.ini run.py ./
COPY migrations ./migrations

RUN addgroup --system app && adduser --system --ingroup app app \
    && mkdir -p /app/data/media \
    && chown -R app:app /app
USER app

EXPOSE 8000
CMD ["python", "run.py"]
