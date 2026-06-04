.PHONY: install test lint typecheck check doctor web run fmt

install:
	pip install -r requirements.txt
	pip install ruff mypy

test:
	pytest -q

lint:
	ruff check bot tests main.py

fmt:
	ruff check --fix bot tests main.py

typecheck:
	mypy bot || true

check: lint test   ## run lint + tests (what CI runs)

doctor:
	python main.py --doctor

web:
	python main.py --web --demo

run:
	python main.py
