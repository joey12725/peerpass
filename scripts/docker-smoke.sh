#!/usr/bin/env bash
# Builds the image, starts the compose stack with the echo host, and runs one visitor request.
set -euo pipefail
cd "$(dirname "$0")/.."
printf '[peerpass]\nhost_name = "Atlas"\nowner_name = "Alex"\n' > peerpass.toml
trap 'docker compose down -v >/dev/null 2>&1; rm -f peerpass.toml' EXIT
docker compose up -d --build --wait
token=$(docker compose exec -T peerpass peerpass issue delegate "smoke test" --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')
export PEERPASS_URL=http://127.0.0.1:8750 PEERPASS_TOKEN="$token"
python3 src/peerpass/client.py ask "ping from CI" --wait 20 | tee /dev/stderr | grep -q '"status": "completed"'
docker compose exec -T peerpass peerpass audit | grep -q completed
echo "docker smoke test passed"
