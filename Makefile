.PHONY: setup ingest server agent up down logs

# `uv run` is the only interpreter spelling that works everywhere: the venv holds
# bin/python on Unix and Scripts/python.exe on Windows, and a relative path with
# forward slashes is not a runnable command under cmd.exe.
PYTHON ?= uv run python
HOST ?= 127.0.0.1
PORT ?= 8732
# --reload watches tracker/ and web/ so editing source rebuilds the running process.
# Set RELOAD=0 to opt out (e.g. when running under launchd).
RELOAD ?= 1
RELOAD_FLAGS := $(if $(filter 0,$(RELOAD)),,--reload --reload-dir tracker --reload-dir web)

setup:
	uv sync
	uv run pip install -e .

ingest:
	$(PYTHON) -m tracker.ingest -v

agent:
	./scripts/install-launchagent.sh

server:
	$(PYTHON) -m uvicorn tracker.api:app --host $(HOST) --port $(PORT) $(RELOAD_FLAGS)

# Ensure the periodic-ingest launchd agent is loaded, then serve the UI.
up: agent server

down:
	./scripts/uninstall-launchagent.sh

logs:
	tail -f ~/Library/Logs/token-tracker.log

# Persistent HTTP server, independent of the periodic ingestion agent.
.PHONY: server-service server-start server-stop server-restart server-status server-uninstall
server-service:
	.venv/bin/python scripts/manage-server-service.py install
server-start:
	.venv/bin/python scripts/manage-server-service.py start
server-stop:
	.venv/bin/python scripts/manage-server-service.py stop
server-restart:
	.venv/bin/python scripts/manage-server-service.py restart
server-status:
	.venv/bin/python scripts/manage-server-service.py status
server-uninstall:
	.venv/bin/python scripts/manage-server-service.py uninstall
