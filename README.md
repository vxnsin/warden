<img src="https://raw.githubusercontent.com/vxnsin/warden/main/assets/wordmark.svg" alt="warden" width="260">

**Nothing binds a port without asking.**

[![PyPI](https://img.shields.io/pypi/v/warden-ports?color=2be0d6&labelColor=0e1a1c&label=pypi)](https://pypi.org/project/warden-ports/)
[![Python](https://img.shields.io/pypi/pyversions/warden-ports?color=6d8687&labelColor=0e1a1c)](https://pypi.org/project/warden-ports/)
[![CI](https://img.shields.io/github/actions/workflow/status/vxnsin/warden/ci.yml?branch=main&color=4fd98c&labelColor=0e1a1c&label=ci)](https://github.com/vxnsin/warden/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-MIT-a87fe0?labelColor=0e1a1c)](LICENSE)

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

## Install

```sh
uv tool install warden-ports        # the `warden` command, anywhere
uvx --from warden-ports warden ports  # or just once, without installing
```

The distribution is called `warden-ports` because `warden` on PyPI belongs to
something else. The command it installs is `warden` either way.
[Installation](https://github.com/vxnsin/warden/wiki/Installation) covers pipx,
pip and a checkout.

## What it does

| | |
| --- | --- |
| **Hands out ports** | A name asks, a port comes back, and it stays that port. [One machine](https://github.com/vxnsin/warden/wiki/One-machine) |
| **Shows what is listening** | `warden ports` reads the machine, not the registry — no server needed. [Ports and processes](https://github.com/vxnsin/warden/wiki/Ports-and-processes) |
| **Remembers** | `warden history 8000` answers what had this port last week. |
| **Says it as it happens** | A live event stream, and webhooks for Discord, Slack, Teams or your own endpoint. [Events and webhooks](https://github.com/vxnsin/warden/wiki/Events-and-webhooks) |
| **Writes your proxy config** | `warden export caddy` turns the registry into a Caddyfile. [Reverse proxy](https://github.com/vxnsin/warden/wiki/Reverse-proxy) |
| **Reads a project file** | `warden.toml` says which ports a project needs; `warden apply` makes it true. [Projects](https://github.com/vxnsin/warden/wiki/Projects) |
| **Spans machines** | One hub, many wardens, one view. [Cluster](https://github.com/vxnsin/warden/wiki/Cluster) |
| **Answers for itself** | `warden doctor` replaces four commands and a guess. [Troubleshooting](https://github.com/vxnsin/warden/wiki/Troubleshooting) |

## A tour

### Ask for a port

```sh
$ warden register shop-api --kind backend --project shop
8000

$ warden ls
SERVICE   KIND     PROJECT  ADDRESS         PID
shop-api  backend  shop     127.0.0.1:8000  14204
```

Ask again tomorrow and it is still 8000. `--preferred-port` wishes for one,
`--require-port` insists and fails if it cannot have it, and
`warden register stack --count 4` takes four at once — all of them or none.

### See what is actually listening

```sh
$ warden ports --port 3000
PORT  PROTO  PROCESS   PID    USER  ADDRESS  WARDEN
3000  tcp    node.exe  25084  dev   0.0.0.0  -

$ warden kill 3000
Stop node.exe (25084) on port 3000? [y/N]: y
```

Neither needs a warden running: they read the machine directly. The WARDEN
column names the service whenever the port did come from the registry, so
anything unmarked arrived some other way.

### Let a project say what it needs

```toml
# warden.toml, beside the code
[project]
name = "shop"

[services.api]
kind = "backend"

[services.web]
kind = "frontend"
preferred_port = 8905
```

```sh
$ warden apply --env .env
SERVICE   KIND      ADDRESS         WHAT
shop-api  backend   127.0.0.1:8900  taken
shop-web  frontend  127.0.0.1:8905  taken
wrote .env
```

Run it again and nothing moves — it renews rather than reshuffling a running
project. A service that cannot get the port it insists on fails the whole run
before anything is written.
[Projects](https://github.com/vxnsin/warden/wiki/Projects) has the whole file
format.

### Hear about it while it happens

```sh
$ warden events
09:41:02  registered   shop-api  127.0.0.1:8600
09:41:44  released     shop-api  127.0.0.1:8600
```

`GET /v1/events` is the same stream as server-sent events. A webhook sends them
somewhere else — `discord`, `slack` and `teams` post something the chat window
renders, and `json` posts the event as it is, signed with an HMAC over exactly
the bytes sent so the far end can tell it really came from you.

Nothing ever waits on a webhook: delivery happens after the change is
committed, off the request path, and `warden doctor` says when the last one did
not arrive — because from the inside, a webhook failing all day looks exactly
like a quiet day.
[Events and webhooks](https://github.com/vxnsin/warden/wiki/Events-and-webhooks)
has where to get an address, and how to check the signature.

### Write the proxy config nobody wants to write by hand

```sh
$ warden export caddy --domain example.com
# Written by `warden export` from the warden on hub. Regenerate it; do not edit it.

shop-api.example.com {
	reverse_proxy 127.0.0.1:8000
}
```

`caddy`, `nginx` and `traefik`. `--all` takes the whole fleet and points each
service at the machine it actually runs on. It prints and stops: nothing is
written in place, and no proxy is reloaded.
[Reverse proxy](https://github.com/vxnsin/warden/wiki/Reverse-proxy) has the
rest.

### Find out why it is not working

```sh
$ warden doctor
ok    warden 0.2.0 answering at http://127.0.0.1:7010, role hub
ok    settings from ~/.config/warden/warden.toml
ok    pool 8000-8999, 3 held, 996 free
warn  1 of 3 registrations held by something that is gone - `warden reap`
ok    events to https://discord.com/... as discord, 12 delivered
```

One command instead of four and a guess. It exits non-zero only on `fail`, so a
warning about an unset token does not make a health check call the machine down.

## Set it up once

```sh
warden setup
```

<img src="https://raw.githubusercontent.com/vxnsin/warden/main/assets/setup.svg" alt="warden setup" width="900">

One screen: which ports to hand out, whether other machines may reach it, which
hub it reports to, where events go, and `ctrl+t` to post a test event before
anything is saved. Questions that nothing has earned stay hidden — no token
field until it listens beyond loopback, no webhook shape until events go
anywhere at all.

| Key | Action |
| --- | --- |
| `tab` `shift+tab` | Move between fields |
| `space` | Toggle a switch or a tick box |
| `enter` | Open a menu, or pick from it |
| `pgup` `pgdn` | Scroll without leaving the field you are in |
| `ctrl+t` | Post a test event to the address on screen |
| `ctrl+s` `ctrl+q` | Save · leave without writing |

It fits an 80 by 24 terminal, which is the size an ssh session usually opens at.
Without a terminal — a script piping answers in, a job on a build machine — the
same questions come one at a time, and `warden setup --plain` asks for that on
purpose. [Configuration](https://github.com/vxnsin/warden/wiki/Configuration)
has every setting there is.

## More than one machine

```sh
# on the hub
warden serve

# on each other machine
WARDEN_UPSTREAM=http://hub:7010 WARDEN_ADVERTISE=http://build-01:7010 warden serve
```

Each warden still hands out its own ports and never waits on the hub. The hub
adds one view over all of them: `warden ls --all`, `warden pool --all`,
`warden tui --all`, and a node that did not answer is named rather than quietly
left out. [Cluster](https://github.com/vxnsin/warden/wiki/Cluster) has the
tokens, the trust rules and what happens when a machine goes quiet.

## From your own code

```python
from warden import reserve

with reserve("shop-api", kind="backend") as port:
    serve(port)          # held while the block runs, released after
```

```sh
PORT=$(warden register shop-api --kind backend)   # or from any shell
```

[Python client](https://github.com/vxnsin/warden/wiki/Python-client) has the
client, the leases and the error types.
[HTTP API](https://github.com/vxnsin/warden/wiki/HTTP-API) has every endpoint,
for everything that is not Python.

## Documentation

The wiki is the long form. This page is the tour.

| Page | For |
| --- | --- |
| [Installation](https://github.com/vxnsin/warden/wiki/Installation) | Getting the `warden` command |
| [One machine](https://github.com/vxnsin/warden/wiki/One-machine) | The usual setup: a registry for your own projects |
| [Ports and processes](https://github.com/vxnsin/warden/wiki/Ports-and-processes) | Seeing and freeing ports, no server needed |
| [Python client](https://github.com/vxnsin/warden/wiki/Python-client) | Asking for a port from your own code |
| [Projects](https://github.com/vxnsin/warden/wiki/Projects) | A `warden.toml` beside the code, and `warden apply` |
| [Events and webhooks](https://github.com/vxnsin/warden/wiki/Events-and-webhooks) | Hearing about it as it happens, in chat or your own endpoint |
| [Reverse proxy](https://github.com/vxnsin/warden/wiki/Reverse-proxy) | Turning the registry into a Caddyfile, nginx or Traefik |
| [Cluster](https://github.com/vxnsin/warden/wiki/Cluster) | Several machines, one hub that knows them all |
| [Docker](https://github.com/vxnsin/warden/wiki/Docker) | The image, a compose file, and what a container can see |
| [Updates](https://github.com/vxnsin/warden/wiki/Updates) | Knowing a new version is out, and rolling it across a fleet |
| [Configuration](https://github.com/vxnsin/warden/wiki/Configuration) | Every setting there is |
| [Command line](https://github.com/vxnsin/warden/wiki/Command-line) | Every command and flag |
| [HTTP API](https://github.com/vxnsin/warden/wiki/HTTP-API) | Endpoints, payloads, status codes |
| [Troubleshooting](https://github.com/vxnsin/warden/wiki/Troubleshooting) | When something does not behave |

## Good to know

- **The registry binds to loopback and has no token by default.** Set
  `WARDEN_TOKEN` before binding it anywhere else.
- **`WARDEN_ALLOW_KILL` is off on purpose.** Stopping processes over the API is
  a much bigger thing to hand out than a port number. `warden kill` on the
  command line acts locally and never asks the API.
- **macOS will not let an unprivileged process enumerate sockets**, so
  `warden ports`, the dashboard's ports view, `warden ls --holders` and
  `warden reap` need `sudo` there. Handing out ports does not.
- **On a Linux server, check that your account lingers.** A systemd user unit
  stops when your last session ends. `warden service install` looks and says so.

<details>
<summary><b>The palette</b>, if you are drawing something that has to match</summary>

It lives in `warden/theme.py`, so the dashboard, the setup screen and the
command line never drift apart.

| Role | Colour | |
| --- | --- | --- |
| Ground | `#08100f` | sculk black |
| Surface | `#0e1a1c` | panels and tables |
| Border | `#1e3538` | |
| Text | `#d9e4e2` | |
| Muted | `#6d8687` | labels, empty cells |
| Live | `#2be0d6` | ports, focus, the banner |
| `frontend` | `#a87fe0` | |
| `worker` | `#e0b457` | also a lease about to run out |
| `database` | `#4fd98c` | also free capacity |
| Conflict | `#e5544b` | expired leases, errors |

</details>

## Development

```sh
git clone https://github.com/vxnsin/warden
cd warden
uv sync --all-groups
uv run pytest
uv run ruff check .
```

The suite runs on Linux, macOS and Windows across Python 3.11, 3.12 and 3.13,
and the Docker image and its three-warden compose file are built and brought up
on every change.

## License

MIT — see [LICENSE](LICENSE).
