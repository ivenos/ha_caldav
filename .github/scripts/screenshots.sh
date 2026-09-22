#!/bin/sh
# Rebuilds the README screenshots from a throwaway Home Assistant and Radicale with sample calendars.
set -eu

PORT="${PORT:-8130}"
NAME="ha_caldav-screenshots-$PORT"
HA="$NAME-ha"
DAV="$NAME-dav"
NETWORK="$NAME"
WORK="${HA_CALDAV_SCREENSHOTS_DIR:-$HOME/.cache/ha_caldav-screenshots-$PORT}"
ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)
HARNESS=$(sed -n 's/^pytest-homeassistant-custom-component==//p' "$ROOT/requirements_test.txt")
: "${HARNESS:?could not be read out of requirements_test.txt}"
HA_VERSION=$(curl -sf "https://pypi.org/pypi/pytest-homeassistant-custom-component/$HARNESS/json" \
    | python3 -c "import json, sys; print(next(r.split('==')[1] for r in json.load(sys.stdin)['info']['requires_dist'] if r.startswith('homeassistant==')))")
HA_IMAGE="${HA_IMAGE:-ghcr.io/home-assistant/home-assistant:$HA_VERSION}"
DAV_IMAGE="${DAV_IMAGE:-$(sed -n 's/.*"\(tomsquest\/docker-radicale\):\$tag".*/\1/p' "$ROOT/tests/live-test.sh"):latest}"
BROWSER_IMAGE="${PLAYWRIGHT_IMAGE:-mcr.microsoft.com/playwright:v1.63.0-noble}"
# The image carries the browsers but not the library, and the two have to be the same release.
BROWSER_VERSION=$(printf '%s' "$BROWSER_IMAGE" | sed -n 's/.*:v\([0-9.]*\).*/\1/p')
case "$DAV_IMAGE" in :*) echo "tests/live-test.sh named no Radicale image" >&2; exit 1 ;; esac

cleanup() {
    docker rm -fv "$HA" "$DAV" >/dev/null 2>&1 || true
    docker network rm "$NETWORK" >/dev/null 2>&1 || true
}
trap cleanup EXIT
trap 'cleanup; exit 130' INT TERM

echo "== start =="
cleanup
mkdir -p "$WORK/browser"
docker network create "$NETWORK" >/dev/null
docker run -d --name "$DAV" --network "$NETWORK" --network-alias dav.example.com "$DAV_IMAGE" >/dev/null
docker create --name "$HA" --security-opt label=disable --network "$NETWORK" \
    --network-alias ha.example.com -p "127.0.0.1:$PORT:8123" "$HA_IMAGE" >/dev/null
docker cp "$ROOT/custom_components" "$HA:/config/custom_components" >/dev/null
docker start "$HA" >/dev/null

BASE="http://127.0.0.1:$PORT"
CLIENT="http://ha.example.com:8123/"
JSON='Content-Type: application/json'

printf 'waiting for startup'
i=0
until curl -sf --max-time 5 "$BASE/api/onboarding" >/dev/null 2>&1; do
    i=$((i + 1))
    [ "$i" -gt 90 ] && { echo; echo "Home Assistant did not come up" >&2; exit 1; }
    printf '.'
    sleep 2
done
echo

echo "== calendars =="
docker exec -i "$HA" python3 - <<'SEED'
import base64
import urllib.request

BASE = "http://dav.example.com:5232/jane/"
AUTH = "Basic " + base64.b64encode(b"jane:screenshots").decode()


def call(method, url, body=b"", content_type="application/xml"):
    request = urllib.request.Request(url, data=body, method=method)
    request.add_header("Authorization", AUTH)
    request.add_header("Content-Type", content_type)
    urllib.request.urlopen(request).close()


def calendar(slug, name, color, events):
    call("MKCALENDAR", BASE + slug + "/", f"""<?xml version="1.0" encoding="utf-8"?>
<C:mkcalendar xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav" xmlns:I="http://apple.com/ns/ical/">
  <D:set><D:prop>
    <D:displayname>{name}</D:displayname>
    <I:calendar-color>{color}</I:calendar-color>
    <C:supported-calendar-component-set><C:comp name="VEVENT"/></C:supported-calendar-component-set>
  </D:prop></D:set>
</C:mkcalendar>""".encode())
    for n, (summary, start, end, extra) in enumerate(events):
        uid = f"{slug}-{n}@example.com"
        when = (
            f"DTSTART;VALUE=DATE:{start}\r\nDTEND;VALUE=DATE:{end}"
            if len(start) == 8
            else f"DTSTART:{start}\r\nDTEND:{end}"
        )
        lines = "".join(f"{line}\r\n" for line in extra)
        body = (
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//ha_caldav//screenshots//EN\r\n"
            f"BEGIN:VEVENT\r\nUID:{uid}\r\nDTSTAMP:20260901T000000Z\r\n{when}\r\n"
            f"SUMMARY:{summary}\r\n{lines}END:VEVENT\r\nEND:VCALENDAR\r\n"
        )
        call("PUT", f"{BASE}{slug}/{n}.ics", body.encode(), "text/calendar")


def todo_list(slug, name, items):
    call("MKCALENDAR", BASE + slug + "/", f"""<?xml version="1.0" encoding="utf-8"?>
<C:mkcalendar xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
  <D:set><D:prop>
    <D:displayname>{name}</D:displayname>
    <C:supported-calendar-component-set><C:comp name="VTODO"/></C:supported-calendar-component-set>
  </D:prop></D:set>
</C:mkcalendar>""".encode())
    for n, (summary, extra) in enumerate(items):
        lines = "".join(f"{line}\r\n" for line in extra)
        body = (
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//ha_caldav//screenshots//EN\r\n"
            f"BEGIN:VTODO\r\nUID:{slug}-{n}@example.com\r\nDTSTAMP:20260901T000000Z\r\n"
            f"SUMMARY:{summary}\r\nX-APPLE-SORT-ORDER:{n}\r\n{lines}END:VTODO\r\nEND:VCALENDAR\r\n"
        )
        call("PUT", f"{BASE}{slug}/{n}.ics", body.encode(), "text/calendar")


urllib.request.urlopen(urllib.request.Request(BASE, method="PROPFIND", headers={"Authorization": AUTH, "Depth": "0"})).close()

calendar("work", "Work", "#3b82f6", [
    ("Team sync", "20260928T093000Z", "20260928T100000Z", [
        "RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR;UNTIL=20261218T093000Z",
        "LOCATION:Room 4.12",
        "DESCRIPTION:Status round with the whole team.\\nJoin: https://meet.example.com/team-sync",
    ]),
    ("1:1 with Alex", "20260929T110000Z", "20260929T113000Z", ["RRULE:FREQ=WEEKLY;BYDAY=TU"]),
    ("Sprint review", "20261002T140000Z", "20261002T150000Z", ["RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=FR"]),
    ("Quarterly planning", "20261021T130000Z", "20261021T160000Z", []),
    ("Customer workshop", "20261027", "20261029", []),
])
calendar("family", "Family", "#10b981", [
    ("Dinner with parents", "20261004T180000Z", "20261004T200000Z", []),
    ("Dentist", "20261008T080000Z", "20261008T084500Z", []),
    ("Parent-teacher meeting", "20261015T170000Z", "20261015T173000Z", []),
    ("Dinner with parents", "20261018T180000Z", "20261018T200000Z", []),
    ("Weekend at the lake", "20261030", "20261102", []),
])
calendar("sports", "Sports", "#f59e0b", [
    ("Running club", "20260929T183000Z", "20260929T193000Z", ["RRULE:FREQ=WEEKLY;BYDAY=TU,TH"]),
    ("Football match", "20261010T150000Z", "20261010T170000Z", []),
    ("Climbing", "20261024T100000Z", "20261024T120000Z", []),
])
calendar("birthdays", "Birthdays", "#ec4899", [
    ("Mia's birthday", "20261009", "20261010", ["RRULE:FREQ=YEARLY"]),
    ("Grandpa's birthday", "20261023", "20261024", ["RRULE:FREQ=YEARLY"]),
])
calendar("travel", "Travel", "#8b5cf6", [
    ("Train to Hamburg", "20261005T070000Z", "20261005T100000Z", []),
    ("Lisbon", "20261104", "20261107", []),
])
todo_list("household", "Household", [
    ("Pay the electricity bill", ["DUE;VALUE=DATE:20261012"]),
    ("Water the plants", ["DTSTART;VALUE=DATE:20261014", "DUE;VALUE=DATE:20261014", "RRULE:FREQ=WEEKLY", "DESCRIPTION:Balcony and living room"]),
    ("Take out the recycling", ["DTSTART;VALUE=DATE:20261015", "DUE;VALUE=DATE:20261015", "RRULE:FREQ=WEEKLY;INTERVAL=2"]),
    ("Call the plumber", ["DUE:20261014T150000Z", "DESCRIPTION:The kitchen tap is dripping again"]),
    ("Pick up the dry cleaning", ["DUE:20261016T160000Z"]),
    ("Buy a gift for Grandpa", ["DUE;VALUE=DATE:20261022", "DESCRIPTION:He mentioned the new bird guide"]),
    ("Clean the gutters", ["DUE;VALUE=DATE:20261024"]),
    ("Book the car service", ["DUE;VALUE=DATE:20261027", "DESCRIPTION:Ask about winter tires"]),
    ("Renew the passport", ["DESCRIPTION:Appointment at the city office"]),
    ("Descale the coffee machine", ["STATUS:COMPLETED", "COMPLETED:20261012T190000Z"]),
    ("Replace the smoke detector batteries", ["STATUS:COMPLETED", "COMPLETED:20261011T100000Z"]),
    ("Return the library books", ["STATUS:COMPLETED", "COMPLETED:20261010T150000Z"]),
])
todo_list("groceries", "Groceries", [
    ("Oat milk", []),
    ("Apples", []),
    ("Coffee beans", []),
])
SEED

echo "== setup =="
CODE=$(curl -sf -X POST "$BASE/api/onboarding/users" -H "$JSON" \
    -d "{\"client_id\":\"$CLIENT\",\"name\":\"Jane\",\"username\":\"jane\",\"password\":\"screenshots\",\"language\":\"en\"}" \
    | python3 -c "import json, sys; print(json.load(sys.stdin)['auth_code'])")
TOKENS=$(curl -sf -X POST "$BASE/auth/token" -d "grant_type=authorization_code&code=$CODE&client_id=$CLIENT")
TOKEN=$(printf '%s' "$TOKENS" | python3 -c "import json, sys; print(json.load(sys.stdin)['access_token'])")
AUTH="Authorization: Bearer $TOKEN"
curl -sf -X POST "$BASE/api/onboarding/core_config" -H "$AUTH" >/dev/null
curl -sf -X POST "$BASE/api/onboarding/analytics" -H "$AUTH" >/dev/null
curl -sf -X POST "$BASE/api/onboarding/integration" -H "$AUTH" -H "$JSON" \
    -d "{\"client_id\":\"$CLIENT\",\"redirect_uri\":\"$CLIENT\"}" >/dev/null

for entry in $(curl -sf "$BASE/api/config/config_entries/entry" -H "$AUTH" \
    | python3 -c "import json, sys; print(' '.join(e['entry_id'] for e in json.load(sys.stdin) if e['domain'] == 'shopping_list'))"); do
    curl -sf -X DELETE "$BASE/api/config/config_entries/entry/$entry" -H "$AUTH" >/dev/null
done

FLOW=$(curl -sf -X POST "$BASE/api/config/config_entries/flow" -H "$AUTH" -H "$JSON" \
    -d '{"handler":"ha_caldav"}' | python3 -c "import json, sys; print(json.load(sys.stdin)['flow_id'])")
curl -sf -X POST "$BASE/api/config/config_entries/flow/$FLOW" -H "$AUTH" -H "$JSON" \
    -d '{"url":"http://dav.example.com:5232/","username":"jane","password":"screenshots","verify_ssl":true}' \
    | python3 -c "import json, sys; r = json.load(sys.stdin); sys.exit(None if r.get('type') == 'create_entry' else str(r))"

printf 'waiting for the calendar colors'
i=0
until [ "$(docker exec "$HA" python3 -c "
import json
entities = json.load(open('/config/.storage/core.entity_registry'))['data']['entities']
print(sum(1 for e in entities if e['platform'] == 'ha_caldav' and e['options'].get('calendar', {}).get('color')))
" 2>/dev/null)" = 5 ]; do
    i=$((i + 1))
    [ "$i" -gt 60 ] && { echo; echo "the calendars did not get their colors" >&2; exit 1; }
    printf '.'
    sleep 2
done
echo

echo "== shots =="
cat > "$WORK/browser/shots.mjs" <<EOF
import { chromium } from 'playwright';

const BASE = 'http://ha.example.com:8123';
const TOKENS = $TOKENS;
EOF
cat >> "$WORK/browser/shots.mjs" <<'EOF'
const NOW = new Date('2026-10-14T08:00:00Z');
const browser = await chromium.launch({ args: ['--no-sandbox'] });

async function open(scheme) {
    const context = await browser.newContext({
        viewport: { width: 1280, height: 1150 },
        deviceScaleFactor: 1.5,
        locale: 'en-US',
        timezoneId: 'UTC',
        colorScheme: scheme,
    });
    await context.addInitScript(tokens => {
        localStorage.setItem('hassTokens', JSON.stringify(tokens));
        localStorage.setItem('dockedSidebar', JSON.stringify('always_hidden'));
    }, { ...TOKENS, hassUrl: BASE, clientId: BASE + '/', expires: NOW.getTime() + 3600000 });
    const page = await context.newPage();
    await page.clock.setFixedTime(NOW);
    return [context, page];
}

{
    const [context, page] = await open('light');
    await page.goto(BASE + '/calendar');
    const occurrence = page.locator('td[data-date="2026-10-14"] .fc-event', { hasText: 'Team sync' });
    await occurrence.waitFor({ timeout: 60000 });
    await page.getByText('Birthdays', { exact: true }).waitFor();
    await page.waitForTimeout(1500);
    await occurrence.click();
    await page.getByRole('button', { name: 'Edit event' }).click();
    await page.locator('ha-recurrence-rule-editor').waitFor();
    await page.locator('ha-input.location input').fill('Room 5.03');
    await page.keyboard.press('Tab');
    const save = page.getByRole('button', { name: 'Save event' });
    for (let i = 0; !(await save.isEnabled()); i++) {
        if (i > 50) throw new Error('Save event stayed disabled');
        await page.waitForTimeout(100);
    }
    await save.click();
    await page.getByText('This and all future events').waitFor();
    await page.waitForTimeout(1000);
    await page.screenshot({ path: '/out/event-editor.png' });
    await context.close();
}

{
    const [context, page] = await open('dark');
    await page.goto(BASE + '/calendar');
    await page.locator('.fc-event').first().waitFor({ timeout: 60000 });
    await page.evaluate(async () => {
        const hass = document.querySelector('home-assistant').hass;
        const named = name => Object.keys(hass.states).find(id => hass.states[id].attributes.friendly_name === name);
        await hass.callWS({ type: 'lovelace/dashboards/create', url_path: 'home-tasks', title: 'Home', mode: 'storage', show_in_sidebar: true });
        await hass.callWS({ type: 'lovelace/config/save', url_path: 'home-tasks', config: { views: [{
            title: 'Home',
            type: 'sections',
            max_columns: 2,
            sections: [
                { type: 'grid', cards: [{ type: 'todo-list', title: 'Household', entity: named('Household') }] },
                { type: 'grid', cards: [
                    { type: 'todo-list', title: 'Groceries', entity: named('Groceries') },
                    { type: 'calendar', title: 'This week', initial_view: 'listWeek', grid_options: { rows: 10 },
                      entities: ['Work', 'Family', 'Sports', 'Birthdays', 'Travel'].map(named) },
                ] },
            ],
        }] } });
    });
    await page.goto(BASE + '/home-tasks/0');
    await page.getByText('Return the library books').waitFor({ timeout: 60000 });
    await page.getByText('Oat milk').waitFor();
    await page.getByText('Parent-teacher meeting').waitFor();
    await page.waitForTimeout(1500);
    await page.screenshot({ path: '/out/todo-list.png' });
    await context.close();
}

await browser.close();
EOF

docker run --rm --security-opt label=disable --user "$(id -u):$(id -g)" \
    --network "$NETWORK" --ipc=host -e HOME=/tmp \
    -v "$WORK/browser:/b" -v "$ROOT/.github/assets:/out" -w /b "$BROWSER_IMAGE" sh -c \
    "set -e
     [ -f node_modules/.playwright-$BROWSER_VERSION ] || { rm -rf node_modules
       npm install --no-audit --no-fund --silent playwright@$BROWSER_VERSION
       touch node_modules/.playwright-$BROWSER_VERSION; }
     node /b/shots.mjs"

ls -l "$ROOT/.github/assets"
