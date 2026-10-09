FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 POETRY_VIRTUALENVS_CREATE=false
WORKDIR /opt/jobping
RUN pip install --no-cache-dir poetry==2.4.1
COPY pyproject.toml poetry.lock README.md ./
RUN poetry install --only main --no-root --no-interaction --no-ansi
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./
COPY examples/ats_sources.tech_companies.json ./examples/ats_sources.tech_companies.json
RUN useradd --uid 10001 --create-home jobping
USER jobping
CMD ["uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8020", "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1"]
