.PHONY: setup build up down logs status audio-devices diagnose rebuild build-gpu up-gpu gpu-check test

GPU_COMPOSE = docker compose -f compose.yaml -f compose.gpu.yaml

build-gpu:
	$(GPU_COMPOSE) build

up-gpu:
	$(GPU_COMPOSE) up -d --build --force-recreate

gpu-check:
	$(GPU_COMPOSE) run --rm --no-deps voice python -c "import ctranslate2; print('CUDA devices:', ctranslate2.get_cuda_device_count())"

setup:
	./setup.sh

build:
	docker compose build

up:
	docker compose up -d

down:
	docker compose down

logs:
	docker compose logs -f

status:
	docker compose ps

audio-devices:
	docker compose run --rm voice python -c "import sounddevice as sd; print(sd.query_devices())"

diagnose:
	./scripts/diagnose.sh

rebuild:
	docker compose build
	docker compose up -d

# Runs the backend tests in the backend image, against temporary databases.
test:
	./scripts/test.sh
