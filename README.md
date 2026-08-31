<img src="assets/wordmark.svg" alt="warden" width="260">

**Nothing binds a port without asking.**

One place that decides which local port a service runs on. Services register
under a name, say what they are, and get a port back. The same name keeps the
same port across restarts, so a backend never wakes up on the port its frontend
grabbed while it was down.

<img src="assets/tui.svg" alt="The warden dashboard" width="900">

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
uv tool install warden
```

Or run it without installing:

```sh
uvx warden serve
```

From a checkout:

```sh
git clone https://github.com/vxnsin/warden
cd warden
uv sync
uv run warden serve
```

## Quick start

Start the registry — it listens on `127.0.0.1:7010` and hands out `8000-8999`:

```sh
warden serve
```

Claim a port:

```sh
$ warden register shop-api --kind backend --project shop
8000
$ warden register shop-web --kind frontend --project shop
8001
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

## Dashboard

```sh
warden tui
```

A live table of every registration, refreshed every two seconds.

| Key | Action |
| --- | --- |
| `↑` `↓` `j` `k` | Move |
| `r` | Reload now |
| `d` | Release the selected service |
| `q` | Quit |

## From Python

The package ships a client, so a service can ask for its own port at startup:

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
| `GET` | `/v1/pool` | Pool size, allocated, free, reserved |
| `GET` | `/v1/services` | List registrations, filter by `project` and `kind` |
| `POST` | `/v1/services` | Register a service, `201` when new, `200` when renewed |
| `GET` | `/v1/services/{name}` | Look up one service |
| `POST` | `/v1/services/{name}/heartbeat` | Extend a lease |
| `DELETE` | `/v1/services/{name}` | Release a port |

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

`POST /v1/services/{name}/heartbeat` pushes the expiry out again. Sent without a
`ttl` it renews the lease the service registered with, so a heartbeat can never
turn a lease into a permanent registration by accident. Expired registrations are
dropped on the next request that touches the registry.

## Configuration

Every setting is an environment variable prefixed `WARDEN_`, or a line in a
`.env` file next to the process.

| Variable | Default | Meaning |
| --- | --- | --- |
| `WARDEN_HOST` | `127.0.0.1` | Interface the registry listens on |
| `WARDEN_PORT` | `7010` | Port the registry listens on |
| `WARDEN_POOL_START` | `8000` | First port that may be handed out |
| `WARDEN_POOL_END` | `8999` | Last port that may be handed out |
| `WARDEN_RESERVED` | empty | Ports to keep out, e.g. `8080,8443,9000-9010` |
| `WARDEN_DATABASE` | platform data dir | SQLite file holding the registry |
| `WARDEN_PROBE` | `true` | Test ports for existing listeners |
| `WARDEN_TOKEN` | empty | Require `Authorization: Bearer <token>` |
| `WARDEN_URL` | `http://127.0.0.1:7010` | Registry the client and CLI talk to |

The registry binds to loopback and has no authentication by default. Set a token
before binding it to anything else.

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
