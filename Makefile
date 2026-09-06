.PHONY: setup ingest server agent up down logs

setup:
	uv sync
	uv run pip install -e .

ingest:
	.venv/bin/python -m tracker.ingest -v

agent:
	./scripts/install-launchagent.sh

server:
	./scripts/run-server.sh

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
