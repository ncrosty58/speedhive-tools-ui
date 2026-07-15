# Install virtual environment and dependencies (including test/lint tools)
install:
    python -m venv venv
    ./venv/bin/pip install --upgrade pip
    ./venv/bin/pip install ruff pytest
    ./venv/bin/pip install -r requirements.txt
    ./venv/bin/pip install -e ./speedhive-tools

# Run the test suite
test:
    ./venv/bin/pytest

# Run the lint checks
lint:
    ./venv/bin/ruff check .

# Run the local Flask server
run:
    ./venv/bin/flask run --host=0.0.0.0 --port=8854

# Clean cache directories
clean:
    find . -type d -name "__pycache__" -exec rm -r {} +
    rm -rf .pytest_cache
