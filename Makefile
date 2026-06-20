.PHONY: install test run clean

install:
	python -m venv venv
	./venv/bin/pip install --upgrade pip
	./venv/bin/pip install -r requirements.txt
	./venv/bin/pip install -e ./speedhive-tools

test:
	./venv/bin/pytest

run:
	./venv/bin/flask run --host=0.0.0.0 --port=8854

clean:
	find . -type d -name "__pycache__" -exec rm -r {} +
	rm -rf .pytest_cache
