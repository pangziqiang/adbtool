run:
	venv/bin/python -m adbtool.main

test:
	venv/bin/python -m unittest discover tests -v

lint:
	venv/bin/python -m ruff check src tests
