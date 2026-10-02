.PHONY: up down logs dev test fmt

up:            ## build and run app + postgres
	docker compose up --build -d
	@echo "app on http://localhost:8000"

down:
	docker compose down -v

logs:
	docker compose logs -f app

dev:           ## run the app locally against compose postgres (needs: make db)
	uvicorn app.main:app --reload --port 8000

db:
	docker compose up -d db

test:
	pytest -q
