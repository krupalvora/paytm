.PHONY: up down logs dev db test burst

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

URL ?= http://localhost:8000
burst:         ## on-sale stampede: make burst URL=https://... ADMIN_TOKEN=...
	./burst.sh $(URL) $(ARGS)
