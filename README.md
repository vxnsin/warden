<img src="https://raw.githubusercontent.com/vxnsin/warden/main/assets/wordmark.svg" alt="warden" width="260">

**Nothing binds a port without asking.**

[Wiki](https://github.com/vxnsin/warden/wiki) ·
[Installation](https://github.com/vxnsin/warden/wiki/Installation) ·
[One machine](https://github.com/vxnsin/warden/wiki/One-machine) ·
[Cluster](https://github.com/vxnsin/warden/wiki/Cluster) ·
[Troubleshooting](https://github.com/vxnsin/warden/wiki/Troubleshooting)

One place that decides which local port a service runs on. Services register
under a name, say what they are, and get a port back. The same name keeps the
same port across restarts, so a backend never wakes up on the port its frontend
grabbed while it was down.

```sh
$ warden run -- npm run dev
shop-api  ->  8000

  VITE ready, listening on http://localhost:8000
```

Nothing to change in the project: the port arrives as `PORT`, is held while the
process runs, and goes back when it exits.

<img src="https://raw.githubusercontent.com/vxnsin/warden/main/assets/tui.svg" alt="The warden dashboard" width="900">

## What is on port 3000?

Not every port on a machine came from a registry. `warden ports` shows every
socket the operating system reports, whether warden handed it out or not, and
`warden kill` frees one:

```sh
$ warden ports --port 3000
PORT  PROTO  PROCESS   PID    USER              ADDRESS  WARDEN
3000  tcp    node.exe  25084  dev               0.0.0.0  -

$ warden kill 3000
Stop node.exe (25084) on port 3000? [y/N]: y
stopped node.exe (25084)
```

Neither needs a warden running anywhere — they read the machine directly. The
WARDEN column names the service whenever the port did come from the registry.
`warden ports --all` asks every warden in the fleet instead, and adds a NODE
column saying which machine each socket is on.

Sockets owned by another user appear without a process name; run warden as
administrator on Windows, or with `sudo` on Linux and macOS, to see those too.

## Why

On a machine that runs a handful of projects, ports are picked by hand and
written down in three places: a `.env`, a `vite.config.ts`, and someone's memory.
Two services eventually pick 8080 and the second one fails to start — or worse,
starts and talks to the wrong neighbour.

`warden` replaces that with a registry:

- every service asks for a port instead of hardcoding one
- ports come from a single pool, so two services cannot collide
- a service keeps its port across restarts
- ports already occupied by something outside the registry are skipped
- `warden ls` answers "what is running on 8003?"

## Install

```sh
uv tool install warden-ports
```

That gives you `warden` in cmd, PowerShell and any POSIX shell, from any
directory. Or run it once without installing:

```sh
uvx --from warden-ports warden ports
```

The distribution is called `warden-ports` because `warden` on PyPI belongs to
something else. The command it installs is `warden` either way.

From a checkout, to work on it:

```sh
git clone https://github.com/vxnsin/warden
cd warden
uv sync
uv run warden
```

## Quick start

Start the registry — it listens on `127.0.0.1:7010` and hands out `8000-8999`:

```sh
warden serve
```

Start something on a port it picks:

```sh
$ warden run --name shop-api --kind backend -- ./server
shop-api  ->  8000
```

Or claim one by hand:

```sh
$ warden register shop-api --kind backend --project shop
8000
$ warden register shop-web --kind frontend --project shop
8001
```

For anything that cannot be wrapped — an IDE run configuration, a Makefile —
`warden env` prints the same claim instead:

```sh
$ warden env shop-api --kind backend
PORT=8000
WARDEN_PORT=8000
WARDEN_ADDRESS=127.0.0.1:8000
```

```sh
eval $(warden env shop-api --export)
warden env shop-api --write .env
```

See who holds what:

```sh
$ warden ls
SERVICE   KIND      PROJECT  ADDRESS         PID
shop-api  backend   shop     127.0.0.1:8000  -
shop-web  frontend  shop     127.0.0.1:8001  -
```

Give a port back:

```sh
warden release shop-web
```

## When a port is held by something that is gone

A registration outlives the process that asked for it, which is how a registry
quietly turns into a list nobody trusts:

```sh
$ warden ls --holders
SERVICE   KIND      PROJECT  ADDRESS         PID    HOLDER
shop-api  backend   shop     127.0.0.1:8000  14204  running
old-job   worker    -        127.0.0.1:8002  9930   gone

$ warden reap
release old-job? nothing is on 8002 and pid 9930 is gone [y/N]: y
released 1
```

A holder is gone when the process it named no longer exists, or when nothing is
listening on its port. Nothing is ever reclaimed on a timer: a service in the
middle of a restart would lose its port to one, so `warden reap` is a person's
decision.

## What used to be on this port

```sh
$ warden history 8000
WHEN    WHAT        SERVICE   KIND     ADDRESS         PID
7s ago  released    shop-api  backend  127.0.0.1:8000  14204
2h ago  registered  shop-api  backend  127.0.0.1:8000  14204
```

Every registration, renewal, move, release and expiry is written down as it
happens, so this still answers for a service released weeks ago.
`warden history shop-api` follows one service instead of one port.

## Hearing about it as it happens

`warden history` answers afterwards. This answers while it is going on:

```sh
$ warden events
09:41:02  registered   shop-api  127.0.0.1:8600
09:41:44  released     shop-api  127.0.0.1:8600
```

`warden events --json` writes one event per line and flushes each as it
arrives, so it pipes into anything. `GET /v1/events` is the same stream as
server-sent events, behind the same token as every other read.

A webhook sends the same events somewhere else. `warden setup` asks for one and
posts a test event, so you find out there and then whether it arrives. In a
terminal that is a screen with a menu and tick boxes; anywhere else, and with
`--plain`, the same questions come one at a time:

```
$ warden setup
Post events to a chat or a service? [y/N]: y
  Anyone holding this address can post as you, so it belongs here and nowhere else.
  Address to post to []: https://discord.com/api/webhooks/...
  Shape it should take (json/discord/slack/teams) [json]: discord
  Events worth posting (registered, renewed, moved, released, expired) [...]: registered,released
  Post a test event now? [Y/n]: y
  It arrived.
```

The same four settings one at a time, into the same file:

```sh
warden settings set webhook https://discord.com/api/webhooks/...
warden settings set webhook_format discord    # or slack, teams, json
warden settings set webhook_events registered,released
warden webhook --test
```

`warden webhook` says where events go and how that has been going; `--test`
posts one made-up event from this machine, which is the quickest way to find
out whether an address still works.

`discord`, `slack` and `teams` post something the chat window renders as a
message rather than a wall of JSON. `json` posts the event as it is, which is
what anything custom should read, and signs it:

```
X-Warden-Signature: sha256=b1646dcf...
```

That is an HMAC over exactly the bytes that were sent, keyed with
`WARDEN_WEBHOOK_SECRET`, so the far end can tell a post really came from this
warden and not from whoever else found the address.

Renewals are left out by default. A channel told about every heartbeat is a
channel people mute within the week.

**Nothing waits on a webhook.** Delivery happens after the change is committed
and off the request path, retried three times and then given up on — a chat
server having a bad afternoon can never make a port take longer to hand out.
`warden doctor` says when the last one did not arrive, because from the inside
a webhook that has been failing all day looks exactly like a quiet day.

## Putting a proxy in front of it

warden already knows every service by name and port, which is the whole of what
a reverse proxy in front of them needs:

```sh
$ warden export caddy --domain example.com
# Written by `warden export` from the warden on hub. Regenerate it; do not edit it.

shop-api.example.com {
	reverse_proxy 127.0.0.1:8000
}
```

`caddy`, `nginx` and `traefik`. `--project` and `--kind` narrow it down, `--all`
takes the whole fleet and points each service at the machine it actually runs
on, and a service carrying a `domain` in its metadata keeps that name whatever
`--domain` says.

The header carries no timestamp on purpose. This output belongs in a
repository, and a line that changes every run turns every regeneration into a
diff worth reviewing.

**It prints and stops.** Nothing is written in place, no proxy is reloaded, and
where the file belongs is not warden's decision. A machine that could not be
asked is named on stderr, so it can never end up in the file you redirected
this into and can never be missed either.

## When something is not working

```sh
$ warden doctor
ok    warden 0.1.0 answering at http://127.0.0.1:7010, role hub
ok    settings from ~/.config/warden/warden.toml
warn  listening on 0.0.0.0 with no token set - anyone who can reach this
      machine can hand out and release ports
ok    pool 8000-8999, 4 held, 995 free
warn  2 of 6 registrations held by something that is gone - warden reap
ok    build-01 online, last seen 4s ago
```

One command instead of four. It exits `1` only when something failed and `0` on
warnings, so it drops into a health check without an unset token being read as
the machine being down.

## Starting it with the machine

```sh
warden service install
```

A systemd user unit on Linux, a launchd agent on macOS, a command in the Startup
folder on Windows. It prints the whole thing before writing it, and
`warden service uninstall` takes it away again. Always as the account that ran
it — a warden started by root or SYSTEM would hand out ports from a registry
nobody else can see.

## Asking for a particular port

Two different wishes, two different fields:

```sh
# "I would like 3000, but anything free will do."
warden register shop-web --kind frontend --preferred-port 3000

# "It has to be 3000, this port is hardcoded in a config I cannot change."
warden register legacy-crm --kind backend --require-port 3000
```

`--preferred-port` falls back to the pool when the port is taken, reserved, or
already in use. `--require-port` fails with `409` instead. Both may name a port
outside the pool, which is how a legacy service on `3000` joins the registry.

## A project that says which ports it needs

Which services a project has tends to live in whichever start script somebody
wrote, and nowhere else. A `warden.toml` beside the code says it once, in
something that gets committed and reviewed:

```toml
[project]
name = "shop"

[services.api]
kind = "backend"

[services.worker]
kind = "worker"

[services.web]
kind = "frontend"
preferred_port = 8905
```

```sh
$ warden apply
SERVICE      KIND      ADDRESS         WHAT
shop-api     backend   127.0.0.1:8900  taken
shop-worker  worker    127.0.0.1:8901  taken
shop-web     frontend  127.0.0.1:8905  taken
```

Run it again and it says `renewed` three times and changes nothing. It renews
what is there; it never shuffles a running project onto different ports.

`warden apply --env .env` writes the ports where the code can read them:

```sh
# Written by `warden apply` from warden.toml. Regenerate it; do not edit it.
SHOP_API_HOST=127.0.0.1
SHOP_API_PORT=8900
SHOP_WORKER_HOST=127.0.0.1
SHOP_WORKER_PORT=8901
```

The whole file is rewritten every time and says so, because the one thing
certain to happen otherwise is somebody editing it by hand and losing it.

`warden apply --release` gives the project's ports back.

**A half-registered project is not a state that exists.** Services that insist
on a particular port are registered first, since those are the ones that can
refuse the whole run — and if anything does fail, what the run took, the run
gives back before it stops.

## More than one port at once

A stack that needs four ports can ask four times and hope nothing takes one in
between, or it can ask once:

```sh
$ warden register stack --kind backend --count 4
8800
8801
8802
8803
```

They come back as `stack-1` to `stack-4`, chosen and written under one lock, so
either all four are held or none are. Asking again renews the same four rather
than shuffling a running stack onto different ports.

`--contiguous` insists they run back to back, for the tools that will not take
a scattered set. When no run is long enough it says so and writes nothing,
rather than handing back four ports that are not what was asked for:

```sh
$ warden register row --kind backend --count 6 --contiguous
no run of 6 free ports in 8800-8809 on 127.0.0.1
```

`warden pool` says it before it comes to that, whenever the two numbers differ:

```sh
$ warden pool
8800-8809  5 allocated  5 free  0 reserved  4 in a row
```

Five ports free, and the longest stretch of them in a row is four. Saying only
"five free" would hide exactly the thing a contiguous request cares about.

## Dashboard

```sh
warden tui
```

Two live tables, refreshed every two seconds. `tab` swaps between what warden
handed out and what is actually listening:

<img src="https://raw.githubusercontent.com/vxnsin/warden/main/assets/tui-ports.svg" alt="The listening ports view" width="900">

| Key | Action |
| --- | --- |
| `↑` `↓` `j` `k` | Move |
| `tab` | Switch between services and ports |
| `n` | Step the filter through one node at a time (with `--all`) |
| `r` | Reload now |
| `d` | Release the service, or stop the process |
| `q` | Quit |

The dashboard reads both tables from the warden it is pointed at, so the ports
it lists are the ones on *that* machine. Stopping a process from here goes
through the API and needs `WARDEN_ALLOW_KILL` (see below); `warden kill` on the
command line is local and always works.

`warden tui --all` points it at the whole fleet instead: both tables gain a NODE
column, `n` steps through one warden at a time, and a node that did not answer
is named at the bottom rather than being quietly left out. Releasing and
stopping go to the machine the row is on — a pid means nothing anywhere else.
Every refresh asks every node, so a large fleet is worth a longer `--interval`.

## From Python

The package ships a client, so a service can ask for its own port at startup.
Full usage on the
[Python client](https://github.com/vxnsin/warden/wiki/Python-client) page:

```python
import uvicorn
from warden import register

port = register("shop-api", kind="backend", project="shop")
uvicorn.run(app, port=port)
```

Look up a neighbour instead of hardcoding its address:

```python
from warden import WardenClient

with WardenClient() as client:
    backend = client.lookup("shop-api")
    base_url = f"http://{backend.address}"
```

For short-lived processes, `reserve` hands the port back on the way out:

```python
from warden import reserve

with reserve("test-fixture", kind="worker") as port:
    run_server(port)
```

## From the shell

```sh
PORT=$(warden register shop-api --kind backend)
exec ./server --port "$PORT"
```

## HTTP API

Base URL `http://127.0.0.1:7010`. Interactive docs at `/docs`.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Liveness and number of registrations |
| `GET` | `/metrics` | Prometheus metrics, behind the same token as every read |
| `GET` | `/v1/pool` | Pool size, allocated, free, reserved |
| `GET` | `/v1/services` | List registrations, filter by `project` and `kind`, `holders=true` for whether each is still there |
| `GET` | `/v1/history` | What happened, filter by `port` and `name` |
| `GET` | `/v1/events` | What is happening, as server-sent events, until you hang up |
| `GET` | `/v1/webhook` | Where events are posted and whether they arrive |
| `POST` | `/v1/services` | Register a service, `201` when new, `200` when renewed |
| `POST` | `/v1/groups` | Register several ports for one thing, all of them or none |
| `GET` | `/v1/services/{name}` | Look up one service |
| `POST` | `/v1/services/{name}/heartbeat` | Extend a lease |
| `DELETE` | `/v1/services/{name}` | Release a port |
| `GET` | `/v1/listeners` | Every socket bound on that machine |
| `DELETE` | `/v1/listeners/{pid}` | Stop a process, off unless `WARDEN_ALLOW_KILL` |
| `POST` | `/v1/nodes` | A warden announces itself, cluster token |
| `GET` | `/v1/nodes` | Every warden this one knows |
| `DELETE` | `/v1/nodes/{name}` | Forget a warden |
| `GET` | `/v1/fleet/services` | Everything the fleet holds, plus what did not answer |
| `GET` | `/v1/fleet/services/{node}/{name}` | One service on one named node |
| `GET` | `/v1/fleet/pool` | How much of its pool every node has left |
| `GET` | `/v1/fleet/listeners` | Every socket bound anywhere in the fleet |
| `POST` | `/v1/fleet/services/{node}` | Register on one named node, through this one |
| `POST` | `/v1/fleet/services/{node}/{name}/heartbeat` | Extend a lease on one named node |
| `DELETE` | `/v1/fleet/services/{node}/{name}` | Release a port on one named node |
| `DELETE` | `/v1/fleet/listeners/{node}/{pid}` | Stop a process on one named node |
| `GET` | `/v1/update` | Whether a newer warden exists |
| `POST` | `/v1/update` | Ask this warden to update itself |
| `POST` | `/v1/fleet/update` | Ask every warden in the fleet to update itself |

```sh
curl -s localhost:7010/v1/services \
  -H 'content-type: application/json' \
  -d '{"name": "shop-api", "kind": "backend", "project": "shop"}'
```

```json
{
  "name": "shop-api",
  "kind": "backend",
  "project": "shop",
  "host": "127.0.0.1",
  "port": 8000,
  "pid": null,
  "meta": {},
  "ttl": null,
  "created_at": "2026-08-31T12:00:00Z",
  "updated_at": "2026-08-31T12:00:00Z",
  "expires_at": null
}
```

Failures come back as `{"detail": "..."}` with `404` for an unknown service,
`409` when a required port is taken, and `503` when the pool is full.

## How a port is chosen

1. A registration that already exists keeps its port, unless another
   registration has taken it meanwhile.
2. `require_port` is granted if it is free and refused with `409` if it is not.
3. `preferred_port` is granted if it is free, and otherwise quietly gives way to
   the pool.
4. Otherwise the lowest free port in the pool wins.
5. Before a fresh port is handed out it is tested for an existing listener, so
   anything started outside the registry is skipped. A service keeping its own
   port is not probed, since it may still be bound to it. `--no-probe` turns the
   test off entirely.

Ports are tracked per host, so `10.0.0.5:8000` and `127.0.0.1:8000` are two
different endpoints.

## Leases

A registration lasts until it is released. Pass `ttl` to make it expire instead —
useful for test fixtures and CI, where nothing gets the chance to clean up:

```sh
warden register ci-runner --kind worker --ttl 600
```

`warden heartbeat ci-runner`, or `POST /v1/services/{name}/heartbeat`, pushes the
expiry out again. Sent without a `ttl` it renews the lease the service registered
with, so a heartbeat can never turn a lease into a permanent registration by
accident. Expired registrations are dropped on the next request that touches the
registry.

## More than one machine

A warden can report to another one. The hub then knows every node, what range it
hands out and whether it is still answering:

```sh
# on the hub
WARDEN_CLUSTER_TOKEN=... warden serve

# on each other machine
WARDEN_CLUSTER_TOKEN=... \
WARDEN_NODE=build-01 \
WARDEN_UPSTREAM=http://hub:7010 \
WARDEN_ADVERTISE=http://build-01:7010 \
  warden serve
```

```sh
$ warden nodes --url http://hub:7010
NODE      URL                    POOL       VERSION  STATUS  LAST SEEN
build-01  http://build-01:7010   9000-9099  0.1.0    online  4s ago
web-02    http://web-02:7010     9000-9099  0.1.0    stale   6m ago
```

**Every node owns its own ports.** The hub is a directory, never the owner, and
that is not a detail: whether a port is free can only be answered on the machine
itself, by trying to bind it. Move the decision to the hub and warden loses the
one thing that makes it more than a spreadsheet — and nothing would start
anywhere while the hub is down.

So a node that cannot reach its hub carries on handing out ports and says so in
its log. A node that stops reporting is shown as `stale` rather than dropped: a
server that is not answering is a fact worth seeing, and
`warden nodes --forget build-01` removes it once it is gone for good.

`WARDEN_ADVERTISE` is the address the hub should use. Leave it out only when both
run on the same machine; a node pointing at a hub elsewhere while advertising
`127.0.0.1` is refused at startup rather than left to fail silently later.

**A name is pinned to the address it first announced.** A second announcement
claiming a different address is refused, because anyone holding the cluster token
could otherwise point an existing node at a machine of their own and collect the
next token the hub forwards. A genuine move is
`warden nodes --forget build-01` first. Set `WARDEN_REQUIRE_HTTPS` once the fleet
can speak it; until then warden names each plain-HTTP node in its log the first
time a token goes there.

The hub can answer for the whole fleet at once, and a node that does not answer
is named rather than left out:

```sh
$ warden ls --all --url http://hub:7010
NODE      SERVICE       KIND     PROJECT  ADDRESS         PID
build-01  build-runner  worker   ci       127.0.0.1:9000  -
hub       hub-api       backend  shop     127.0.0.1:8000  -
build-01 (http://build-01:7010) could not be reached
```

A name is unique per node and never across the fleet, so this view is the only
place a clash can show up at all. Two machines both holding a `shop-api` is
nearly always two projects that drifted apart, and it is said out loud rather
than left to be noticed:

```
shop-api is registered on build-01 and web-02
```

`warden get build-01/build-runner` asks one named node.

`--node` puts a request through the hub to one particular warden, so a machine
can be handed a port without a shell on it:

```sh
$ warden register build-runner --kind worker --node build-01 --url http://hub:7010
9000
$ warden release build-runner --node build-01 --url http://hub:7010
released build-01/build-runner
```

The node still decides. Only the machine itself can try to bind a port, so
probing keeps working exactly as it does locally, and what comes back refused
comes back in that node's own words:

```sh
$ warden register legacy-crm --kind backend --require-port 3000 --node build-01
port 3000 is held by 'grafana'
```

Forwarding takes `WARDEN_TOKEN`, never the cluster token, and the hub carries
the caller's own authorization to the node rather than its own. A hub that
could write with the cluster token would be the one door that token was never
meant to open.

`warden pool --all` does the same for capacity, so the machine about to run out
is the one to look at rather than the one to find:

```sh
$ warden pool --all --url http://hub:7010
NODE      POOL       HELD  FREE  RESERVED
build-01  9000-9099    97     3         0
hub       8000-8999     4   995         1

2 wardens  101 allocated  998 free  of 1099
```

Every node keeps its own range, and two of them may well hand out the same
numbers on different machines, so the total is a sum of what is left and never
one pool the fleet shares.

Wardens authenticate to each other with `WARDEN_CLUSTER_TOKEN`, separate from the
`WARDEN_TOKEN` a person uses. The cluster token opens announcing and reading; it
opens nothing that changes state.

The [Cluster](https://github.com/vxnsin/warden/wiki/Cluster) page goes through
the whole thing: what the hub keeps, what survives what, and why it is built
this way.

## In a container

There is a `Dockerfile` and a `compose.yaml` bringing up a hub with two nodes:

```sh
WARDEN_TOKEN=... WARDEN_CLUSTER_TOKEN=... docker compose up -d
docker compose exec hub warden nodes
```

Every change here builds that image and brings the three of them up in CI: the
healthcheck has to pass, the hub has to see both nodes, and a port registered
through the hub onto a node has to come back in `warden ls --all`.

**A warden in a container sees the container's ports, not the host's.** Probing
and `warden ports` describe the network namespace they run in, so a warden meant
to manage the host's ports needs `network_mode: host` — and is then on the
host's network, where the token is the only thing between it and everyone else
there. The [Docker](https://github.com/vxnsin/warden/wiki/Docker) page has the
rest.

## Updates

```sh
$ warden update
warden 0.2.0 is out, this is 0.1.0

$ warden update --fleet --url http://hub:7010
NODE      RESULT   DETAIL
build-01  updated  Successfully installed warden-0.2.0
db-03     refused  updating over the API is switched off
hub       updated  Successfully installed warden-0.2.0
```

**The hub sends an intent, never a command.** `POST /v1/update` means "update
yourself"; what that does comes from the asked machine's own
`WARDEN_UPDATE_COMMAND` and nowhere else. Otherwise a leaked cluster token would
be worth every machine it can reach. Both `WARDEN_ALLOW_REMOTE_UPDATE` and a
configured command are off by default, and a warden without them refuses and
says so.

The [Updates](https://github.com/vxnsin/warden/wiki/Updates) page has the rest,
including why the restart is your command's job.

## Configuration

```sh
warden setup       # answer a few questions, once
warden settings    # see every value, and where it came from
```

<img src="https://raw.githubusercontent.com/vxnsin/warden/main/assets/setup.svg" alt="warden setup" width="900">

In a terminal, `warden setup` is one screen: tab between the fields, a menu for
the webhook shape, tick boxes for which events are worth posting, and `ctrl+t`
to post a test event before saving anything. Questions that nothing has earned
stay hidden - there is no token to fill in until the warden is reachable from
somewhere else, and no shape to pick until events are going anywhere at all.

Without a terminal - a script piping answers in, a job on a build machine - the
same questions come one at a time instead, and `warden setup --plain` asks for
that on purpose.

`warden setup` writes a file in the platform config directory, so a globally
installed warden needs no environment at all. Settings still come from a flag,
the environment or a `.env` beside the process when you want them to, in that
order, and `warden settings` says which one is winning.

[Configuration](https://github.com/vxnsin/warden/wiki/Configuration) has every
setting. Each is also an environment variable with a `WARDEN_` prefix:

| Variable | Default | Meaning |
| --- | --- | --- |
| `WARDEN_HOST` | `127.0.0.1` | Interface the registry listens on |
| `WARDEN_PORT` | `7010` | Port the registry listens on |
| `WARDEN_POOL_START` | `8000` | First port that may be handed out |
| `WARDEN_POOL_END` | `8999` | Last port that may be handed out |
| `WARDEN_RESERVED` | empty | Ports to keep out, e.g. `8080,8443,9000-9010` |
| `WARDEN_DATABASE` | platform data dir | SQLite file holding the registry |
| `WARDEN_PROBE` | `true` | Test ports for existing listeners |
| `WARDEN_ALLOW_KILL` | `false` | Let the API stop processes |
| `WARDEN_TOKEN` | empty | Require `Authorization: Bearer <token>` |
| `WARDEN_URL` | `http://127.0.0.1:7010` | Registry the client and CLI talk to |
| `WARDEN_UPDATE_CHECK` | `true` | Ask GitHub whether a newer release exists |
| `WARDEN_ALLOW_REMOTE_UPDATE` | `false` | Let a caller ask this warden to update itself |
| `WARDEN_UPDATE_COMMAND` | empty | What updating means on this machine |
| `WARDEN_WEBHOOK` | empty | Address events are posted to |
| `WARDEN_WEBHOOK_FORMAT` | `json` | `json`, `discord`, `slack` or `teams` |
| `WARDEN_WEBHOOK_EVENTS` | all but `renewed` | Which events are worth posting |
| `WARDEN_WEBHOOK_SECRET` | empty | Key the posted body is signed with |
| `WARDEN_NODE` | machine name | This warden's name in the fleet |
| `WARDEN_UPSTREAM` | empty | Hub to report to; empty means it is one |
| `WARDEN_ADVERTISE` | from host and port | Address the hub should use to reach it |
| `WARDEN_CLUSTER_TOKEN` | empty | Shared secret between wardens |
| `WARDEN_NODE_TTL` | `90` | Seconds a node's entry stays fresh |
| `WARDEN_REQUIRE_HTTPS` | `false` | Refuse to register or send a token to a plain HTTP node |

The registry binds to loopback and has no authentication by default. Set a token
before binding it to anything else.

`WARDEN_ALLOW_KILL` is off on purpose. A warden reachable from the network would
otherwise let anyone holding the token end processes on that machine, which is a
much bigger thing to hand out than a port number. `warden kill` on the command
line is unaffected: it acts locally and never asks the API.

## Colours

The palette lives in `warden/theme.py`, so the dashboard, the CLI and this page
never drift apart.

| Role | Colour | |
| --- | --- | --- |
| Ground | `#08100f` | sculk black |
| Surface | `#0e1a1c` | panels and table |
| Border | `#1e3538` | |
| Text | `#d9e4e2` | |
| Muted | `#6d8687` | labels, empty cells |
| Live | `#2be0d6` | ports, focus, the banner |
| `frontend` | `#a87fe0` | |
| `worker` | `#e0b457` | also a lease about to run out |
| `database` | `#4fd98c` | also free capacity |
| Conflict | `#e5544b` | expired leases, errors |

## Development

```sh
uv sync --all-groups
uv run pytest
uv run ruff check .
```

## License

MIT
