.PHONY: install test demo serve clean

install:
	python -m pip install -r requirements.txt

test:
	python -m pytest -q

demo:
	python scripts/demo.py

serve:
	APIMON_ADMIN_TOKEN=change-me python -m apimon serve --db build/apimon.db --alerts examples/alerts.json

clean:
	rm -rf build .pytest_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
