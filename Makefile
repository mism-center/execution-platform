.PHONY: install dev run test lint format typecheck clean

install:
	pip install -e ".[dev]"
	pip install -e "../metadata-schema"

dev:
	uvicorn main:app --app-dir src --reload --host 0.0.0.0 --port 8000

run:
	uvicorn main:app --app-dir src --host 0.0.0.0 --port 8000

test:
	pytest -v

lint:
	ruff check src/ tests/

format:
	ruff format src/ tests/

typecheck:
	mypy src/

clean:
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type d -name .pytest_cache -exec rm -rf {} +
	rm -rf .mypy_cache .ruff_cache
