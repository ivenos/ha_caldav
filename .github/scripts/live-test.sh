#!/usr/bin/env bash
set -euo pipefail

server="${1:?usage: live-test.sh <server> [tag] [-- pytest args]}"
shift || true
tag="latest"
if [[ "${1:-}" != "" && "${1:-}" != "--" ]]; then
  tag="$1"
  shift
fi
[[ "${1:-}" == "--" ]] && shift

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/../.." && pwd)"
name="ha-caldav-live-$server"
username="admin"
password='TestPass!2026'

cleanup() { docker rm -f "$name" >/dev/null 2>&1 || true; }
trap cleanup EXIT
cleanup

wait_for() {
  local url="$1" tries="${2:-60}" gap="${3:-3}"
  echo -n "Waiting for $server"
  for _ in $(seq 1 "$tries"); do
    if curl -fsS --max-time 10 -o /dev/null "$url" 2>/dev/null; then
      echo " - ready"
      return 0
    fi
    echo -n "."
    sleep "$gap"
  done
  echo
  echo "::error::$server did not become ready"
  docker logs --tail 50 "$name"
  return 1
}

case "$server" in
  nextcloud)
    docker run -d --name "$name" -p 8080:80 \
      -e SQLITE_DATABASE=nextcloud \
      -e NEXTCLOUD_ADMIN_USER="$username" \
      -e NEXTCLOUD_ADMIN_PASSWORD="$password" \
      -e NEXTCLOUD_TRUSTED_DOMAINS=localhost \
      "nextcloud:$tag" >/dev/null
    echo -n "Waiting for nextcloud to finish installing"
    for _ in $(seq 1 60); do
      if curl -fsS --max-time 10 http://localhost:8080/status.php 2>/dev/null \
        | grep -q '"installed":true'; then
        echo " - ready"
        break
      fi
      echo -n "."
      sleep 5
    done
    curl -fsS --max-time 10 http://localhost:8080/status.php 2>/dev/null \
      | grep -q '"installed":true' || {
      echo "::error::Nextcloud did not finish installing"
      docker logs --tail 50 "$name"
      exit 1
    }
    url="http://localhost:8080/remote.php/dav"
    ;;

  radicale)
    # The default image runs with "auth type = none", which accepts any login
    docker run -d --name "$name" -p 5232:5232 \
      "tomsquest/docker-radicale:$tag" >/dev/null
    wait_for http://localhost:5232/ 40 2
    url="http://localhost:5232/"
    ;;

  xandikos)
    docker run -d --name "$name" -p 8000:8000 \
      "ghcr.io/jelmer/xandikos:$tag" \
      --autocreate --defaults -d /data -l 0.0.0.0 -p 8000 --route-prefix=/ >/dev/null
    wait_for http://localhost:8000/ 40 2
    url="http://localhost:8000/"
    ;;

  *)
    echo "unknown server: $server (known: nextcloud, radicale, xandikos)"
    exit 2
    ;;
esac

cd "$root"
set +e
CALDAV_URL="$url" CALDAV_USERNAME="$username" CALDAV_PASSWORD="$password" \
  pytest -m live "$@"
status=$?
set -e
if [[ $status -ne 0 ]]; then
  echo "::group::$server logs"
  docker logs --tail 100 "$name" || true
  echo "::endgroup::"
fi
exit "$status"
