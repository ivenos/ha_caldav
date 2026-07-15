#!/usr/bin/env bash
# Run the live tests against a throwaway Nextcloud. Nothing leaves this machine
# and the container is removed on exit, including on failure.
#
#   scripts/live-test.sh            # nextcloud:latest
#   scripts/live-test.sh 32         # a specific major
#   scripts/live-test.sh latest -v  # extra args go to pytest
set -euo pipefail

version="${1:-latest}"
shift || true

name="ha-caldav-live"
password='TestPass!2026'
url="http://localhost:8080/remote.php/dav"

cleanup() { docker rm -f "$name" >/dev/null 2>&1 || true; }
trap cleanup EXIT
cleanup

echo "Starting nextcloud:$version"
docker run -d --name "$name" -p 8080:80 \
  -e SQLITE_DATABASE=nextcloud \
  -e NEXTCLOUD_ADMIN_USER=admin \
  -e NEXTCLOUD_ADMIN_PASSWORD="$password" \
  -e NEXTCLOUD_TRUSTED_DOMAINS=localhost \
  "nextcloud:$version" >/dev/null

echo -n "Waiting for the install to finish"
for _ in $(seq 1 60); do
  if curl -fsS http://localhost:8080/status.php 2>/dev/null | grep -q '"installed":true'; then
    echo " - $(curl -fsS http://localhost:8080/status.php | sed -n 's/.*"versionstring":"\([^"]*\)".*/\1/p')"
    break
  fi
  echo -n "."
  sleep 5
done

if ! curl -fsS http://localhost:8080/status.php 2>/dev/null | grep -q '"installed":true'; then
  echo
  echo "Nextcloud did not finish installing:"
  docker logs --tail 30 "$name"
  exit 1
fi

CALDAV_URL="$url" CALDAV_USERNAME=admin CALDAV_PASSWORD="$password" \
  pytest -m live "$@"
