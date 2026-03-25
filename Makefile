.PHONY: dev prod install clean lint test

# Development variables
APP_MODULE = src/main.py

# Start development server with hot-reload
dev:
	uv run $(APP_MODULE)

# Start production server with multiple workers
prod:
	uv run $(APP_MODULE)

# Install Python dependencies via uv
install:
	uv sync

# Format and lint code (assuming ruff is added or will be)
lint:
	uv run ruff check .
	uv run ruff format .

# Basic clean up
clean:
	rm -rf __pycache__ .mypy_cache .pytest_cache
	find . -type d -name __pycache__ -exec rm -rf {} +
