.PHONY: base build up down

base:
	docker build -f Dockerfile.base -t akamai-base:latest .

build: base
	docker build -t akamai-solver:latest .

up: build
	docker compose up -d

down:
	docker compose down -v

