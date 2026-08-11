# JobPing

JobPing is a low-latency job aggregation and discovery engine focused on 2026/2027
technology internships and new-grad roles. It is designed to ingest listings from community
repositories, public applicant-tracking-system APIs, and dynamic employer portals, then
normalize, deduplicate, and publish changes to clients in real time.

## Requirements

- Python 3.12+
- [Poetry](https://python-poetry.org/)

## Local development

Install the project and development tools:

```shell
poetry install
```

Run the quality checks:

```shell
poetry run ruff check .
poetry run black --check .
```

Apply formatting when needed:

```shell
poetry run ruff check --fix .
poetry run black .
```

## Project structure

```text
app/       Python application package
tests/     Automated tests
```

The ingestion, persistence, event delivery, and API layers will be added incrementally as
independent, tested modules.
