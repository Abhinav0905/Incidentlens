.PHONY: install run test lint format typecheck

install:
	pip install -e ".[dev]"

run:
	incidentlens serve

test:
	pytest --cov=incidentlens

lint:
	ruff check .

format:
	ruff format .

typecheck:
	mypy src
