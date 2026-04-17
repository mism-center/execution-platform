VENV := .venv
PY   := $(VENV)/bin/python3

# This target creates the virtual environment, creates the .venv/bin/python3 file, and upgrades pip.
# Simply use this target as a prerequisite if you need the virtual environment to be created.
# Requires Python 3 to be installed on the system.
$(PY):
	python3 -m venv $(VENV)
	$(PY) -m pip install --upgrade pip

.PHONY: install dev run test lint format typecheck clean

install: $(PY)
	$(PY) -m pip install -e ".[dev]"
	$(PY) -m pip install -e "../metadata-schema"

dev: install
	uvicorn main:app --app-dir src --reload --host 0.0.0.0 --port 8000

run: install
	uvicorn main:app --app-dir src --host 0.0.0.0 --port 8000

test: $(PY)
	pytest -v

lint: $(PY)
	ruff check src/ tests/

format: $(PY)
	ruff format src/ tests/

typecheck: $(PY)
	mypy src/

clean: $(PY)
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type d -name .pytest_cache -exec rm -rf {} +
	rm -rf .mypy_cache .ruff_cache
