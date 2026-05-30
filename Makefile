.PHONY: up down logs status reset open clickhouse help

up:
	docker compose up -d
	@echo "Waiting for services to be healthy..."
	@sleep 8
	@docker compose ps

down:
	docker compose down

logs:
	docker compose logs -f

status:
	docker compose ps

reset:
	docker compose down -v
	@echo "All volumes wiped."

open:
	open http://localhost:8501       # Eval UI
	open http://localhost:16686      # Jaeger
	open http://localhost:8123/play  # ClickHouse

clickhouse:
	docker exec -it aieval-clickhouse clickhouse-client --database otel

build:
	docker compose build

restart:
	docker compose restart

help:
	@echo "make up         - Start all services"
	@echo "make down       - Stop all services"
	@echo "make logs       - Tail logs"
	@echo "make status     - Show service health"
	@echo "make reset      - Wipe all data volumes"
	@echo "make open       - Open all UIs in browser"
	@echo "make clickhouse - Open ClickHouse SQL console"
	@echo "make build      - Rebuild Docker images"
