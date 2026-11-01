.PHONY: up down logs test reset shell

up:
	docker compose up --build -d
	@echo "Simulador: http://localhost:8000/simulador  ·  Panel: http://localhost:8000/dashboard"

down:
	docker compose down

logs:
	docker compose logs -f api worker

test:
	-docker compose exec -T db psql -U senda -c "CREATE DATABASE senda_test" 2>/dev/null
	docker compose exec -T api pip install -q pytest
	docker compose exec -T -e DATABASE_URL=postgresql://senda:senda@db:5432/senda_test api python -m pytest -q

reset:
	curl -s -X POST http://localhost:8000/api/demo/reset

shell:
	docker compose exec db psql -U senda senda
