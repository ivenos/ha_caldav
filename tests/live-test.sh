#!/usr/bin/env bash
set -euo pipefail

server="${1:?usage: live-test.sh <server> [tag] [-- pytest args]}"
shift || true
tag="latest"
if [[ -n "${1:-}" && "${1:-}" != -* ]]; then
  tag="$1"
  shift
fi
[[ "${1:-}" == "--" ]] && shift

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/.." && pwd)"
name="ha-caldav-live-$server"
database="$name-db"
network="$name-net"
username="admin"
password='TestPass!2026'

cleanup() {
  docker rm -fv "$name" "$database" >/dev/null 2>&1 || true
  docker network rm "$network" >/dev/null 2>&1 || true
}
trap cleanup EXIT
trap 'cleanup; exit 130' INT TERM
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
    docker run -d --name "$name" -p 127.0.0.1:8080:80 \
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
    docker run -d --name "$name" -p 127.0.0.1:5232:5232 \
      "tomsquest/docker-radicale:$tag" >/dev/null
    wait_for http://localhost:5232/ 40 2
    url="http://localhost:5232/"
    ;;

  xandikos)
    docker run -d --name "$name" -p 127.0.0.1:8000:8000 \
      "ghcr.io/jelmer/xandikos:$tag" \
      --autocreate --defaults -d /data -l 0.0.0.0 -p 8000 --route-prefix=/ >/dev/null
    wait_for http://localhost:8000/ 40 2
    url="http://localhost:8000/"
    ;;

  baikal)
    docker run -d --name "$name" -p 127.0.0.1:8082:80 "ckulka/baikal:$tag" >/dev/null
    wait_for http://localhost:8082/ 40 2
    # Baikal has only an install wizard, so this seeds it through php; only the nginx image has sqlite3.
    # No email on the account, or sabre/dav answers 500 when deleting an event with it as attendee but no organizer.
    docker exec -i -e BAIKAL_USER="$username" -e BAIKAL_PASSWORD="$password" \
      "$name" php >/dev/null <<'PHP'
<?php
$root = "/var/www/baikal";
require "$root/Core/Distrib.php";
$user = getenv("BAIKAL_USER");
$password = getenv("BAIKAL_PASSWORD");
$file = "$root/Specific/db/db.sqlite";

@mkdir(dirname($file), 0770, true);
@unlink($file);
$db = new PDO("sqlite:$file");
$db->setAttribute(PDO::ATTR_ERRMODE, PDO::ERRMODE_EXCEPTION);
// The schema is one file of statements; Baikal's own installer splits it too.
foreach (explode(";", file_get_contents("$root/Core/Resources/Db/SQLite/db.sql")) as $query) {
    if (trim($query) !== "") {
        $db->exec($query);
    }
}
// digesta1 is what both the Digest and the Basic backend compare against.
$db->prepare("INSERT INTO users (username, digesta1) VALUES (?, ?)")
    ->execute([$user, md5("$user:BaikalDAV:$password")]);
$db->prepare("INSERT INTO principals (uri, displayname) VALUES (?, ?)")
    ->execute(["principals/$user", $user]);

file_put_contents("$root/config/baikal.yaml", implode("\n", [
    "system:",
    "    configured_version: '" . BAIKAL_VERSION . "'",
    "    timezone: UTC",
    "    card_enabled: true",
    "    cal_enabled: true",
    "    dav_auth_type: Digest",
    "    admin_passwordhash: " . hash("sha256", "admin:BaikalDAV:$password"),
    "    auth_realm: BaikalDAV",
    "    base_uri: ''",
    "database:",
    "    backend: sqlite",
    "    sqlite_file: $file",
    "",
]));

// The apache and nginx variants serve as different users, and Baikal needs its config writable.
$owner = stat("$root/html");
foreach ([$file, dirname($file), "$root/config/baikal.yaml"] as $path) {
    chown($path, $owner["uid"]);
    chgrp($path, $owner["gid"]);
}
PHP
    url="http://localhost:8082/dav.php/"
    ;;

  sogo)
    docker network create "$network" >/dev/null
    docker run -d --name "$database" --network "$network" \
      -e MARIADB_RANDOM_ROOT_PASSWORD=yes \
      -e MARIADB_DATABASE=sogo \
      -e MARIADB_USER=sogo \
      -e MARIADB_PASSWORD="$password" \
      mariadb:11 >/dev/null
    # Over tcp: the entrypoint's temporary server listens on the socket before the account exists.
    echo -n "Waiting for the sogo database"
    for _ in $(seq 1 60); do
      if docker exec -e MYSQL_PWD="$password" "$database" \
        mariadb -h 127.0.0.1 -usogo sogo -e "SELECT 1" >/dev/null 2>&1; then
        echo " - ready"
        break
      fi
      echo -n "."
      sleep 2
    done
    docker exec -e MYSQL_PWD="$password" "$database" \
      mariadb -h 127.0.0.1 -usogo sogo -e "SELECT 1" >/dev/null 2>&1 || {
      echo "::error::the sogo database did not come up"
      docker logs --tail 50 "$database"
      exit 1
    }
    # SOGo's sql user source expects this table and these column names but never creates them.
    docker exec -e MYSQL_PWD="$password" "$database" mariadb -h 127.0.0.1 -usogo sogo -e "
      CREATE TABLE sogo_users (
        c_uid varchar(64) PRIMARY KEY,
        c_name varchar(64) NOT NULL,
        c_password varchar(64) NOT NULL,
        c_cn varchar(128),
        mail varchar(128));
      INSERT INTO sogo_users VALUES ('$username', '$username',
        MD5('$password'), '$username', '$username@example.com');"

    docker run -d --name "$name" --network "$network" -p 127.0.0.1:8084:80 \
      "pmietlicki/sogo:$tag" >/dev/null
    wait_for http://localhost:8084/SOGo/ 60 3
    # The shipped config has every setting commented out.
    # Mail is off, or SOGo contacts IMAP and SMTP on every write.
    docker exec -i "$name" sh -c \
      'cat > /srv/etc/sogo.conf && install -o root -g sogo -m 640 /srv/etc/sogo.conf /etc/sogo/sogo.conf' <<EOF
{
  SOGoProfileURL = "mysql://sogo:$password@$database:3306/sogo/sogo_user_profile";
  OCSFolderInfoURL = "mysql://sogo:$password@$database:3306/sogo/sogo_folder_info";
  OCSSessionsFolderURL = "mysql://sogo:$password@$database:3306/sogo/sogo_sessions_folder";
  SOGoUserSources = (
    {
      type = sql;
      id = directory;
      viewURL = "mysql://sogo:$password@$database:3306/sogo/sogo_users";
      canAuthenticate = YES;
      isAddressBook = YES;
      userPasswordAlgorithm = md5;
    }
  );
  SOGoTimeZone = "UTC";
  SOGoLanguage = English;
  SOGoMailDomain = "example.com";
  SOGoMemcachedHost = "127.0.0.1";
  SOGoEnableEMailAlarms = NO;
  SOGoAppointmentSendEMailNotifications = NO;
  SOGoACLsSendEMailNotifications = NO;
  SOGoFoldersSendEMailNotifications = NO;
  WOWorkersCount = 3;
  WOPidFile = "/var/run/sogo/sogo.pid";
}
EOF
    # supervisord restarts sogod, which reads its config only at startup.
    docker exec "$name" pkill sogod >/dev/null 2>&1 || true
    echo -n "Waiting for sogo to accept the seeded account"
    for _ in $(seq 1 40); do
      if curl -fsS --max-time 10 -o /dev/null -u "$username:$password" \
        -X PROPFIND -H "Depth: 0" http://localhost:8084/SOGo/dav/ 2>/dev/null; then
        echo " - ready"
        break
      fi
      echo -n "."
      sleep 3
    done
    curl -fsS --max-time 10 -o /dev/null -u "$username:$password" \
      -X PROPFIND -H "Depth: 0" http://localhost:8084/SOGo/dav/ 2>/dev/null || {
      echo "::error::sogo did not accept the seeded account"
      docker logs --tail 50 "$name"
      exit 1
    }
    url="http://localhost:8084/SOGo/dav/"
    ;;

  *)
    echo "unknown server: $server (known: baikal, nextcloud, radicale, sogo, xandikos)"
    exit 2
    ;;
esac

cd "$root"
pytest="$root/.venv/bin/pytest"
[[ -x $pytest ]] || pytest=pytest
set +e
CALDAV_URL="$url" CALDAV_USERNAME="$username" CALDAV_PASSWORD="$password" \
  "$pytest" -m live "$@"
status=$?
set -e
if [[ $status -ne 0 ]]; then
  echo "::group::$server logs"
  docker logs --tail 100 "$name" || true
  echo "::endgroup::"
fi
exit "$status"
