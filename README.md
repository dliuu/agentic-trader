# Whale Scanner

Agent A — deterministic rule engine for unusual options flow. Polls the [Unusual Whales](https://unusualwhales.com) API during market hours, applies configurable filters, and emits candidate alerts for downstream grading.

No LLM. Pure Python: async HTTP, Pydantic models, SQLite persistence.

## Requirements

- Python 3.11+ (use `brew install python@3.11` or [pyenv](https://github.com/pyenv/pyenv) if needed)
- Unusual Whales API token

## Setup

```bash
# Create virtual environment
python3.11 -m venv .venv
source .venv/bin/activate  # or .venv\Scripts\activate on Windows

# Install in editable mode
pip install -e ".[dev]"

# Copy env template and add your API token
cp .env.example .env
# Edit .env and set UW_API_TOKEN
```

## Run

```bash
python -m scanner.main
```

Or:

```bash
whale-scanner
```

The scanner runs during US market hours (9:15–16:00 ET, configurable). Outside hours it sleeps until the next open.

## Config

All tunable parameters live in `config/rules.yaml`: filter thresholds, confluence weights, polling intervals, dedup TTL, etc.

## Tests

```bash
pytest
```

## Docker

```bash
docker compose -f docker/docker-compose.yaml up --build
```

## License

MIT
