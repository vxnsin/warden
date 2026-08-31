# Running warden on more than one machine

One warden looks after one machine. Point several at a common one and you get a
fleet: every node still runs its own show, and the hub knows who exists, what
each hands out, and who has stopped answering.

## The shape

```
   a service                a service                 a service
       |                        |                         |
       v                        v                         v
  warden on              warden on                  warden on
  build-01               web-02                     db-03
  pool 9000-9099         pool 9000-9099             pool 9000-9099
       |                        |                         |
       |  POST /v1/nodes        |                         |
       |  every 30s             |                         |
       +------------------------+-------------------------+
                                |
                                v
                        warden on hub
                        knows all three
                        warden nodes
```

A service always talks to the warden on its own machine. It never needs to know
the hub exists, and nothing it does stops working when the hub does.

## Why the hub is a directory and never the owner

This is the decision everything else follows from, so it is worth being plain
about.

warden does not only track what it handed out. Before giving away a port it
tries to bind it, and skips it if something else got there first — a container,
a service someone started by hand, a stray process from last week. That check
only has an answer **on the machine itself**. From the hub, `10.0.0.7:9000`
either refuses a connection or does not, which says nothing about whether the
port can be bound.

Move allocation into the hub and warden loses the one thing that makes it more
than a shared spreadsheet. It also makes the hub load-bearing: nothing could
start anywhere while it is down.

So allocation stays where the machine is. The hub aggregates.

## What the hub keeps

One row per node, in the same SQLite file as its own registrations:

| Field | Meaning |
| --- | --- |
| `name` | What the node calls itself (`WARDEN_NODE`) |
| `url` | The address the node says to use (`WARDEN_ADVERTISE`) |
| `pool_start`, `pool_end` | The range that node hands out |
| `version` | Which warden it is running |
| `first_seen` | When it first reported, and never overwritten |
| `last_seen` | Its most recent report |
| `expires_at` | When its entry goes stale |

`status` is not stored. It is `online` while `expires_at` is in the future and
`stale` afterwards, so it is always true at the moment you ask.

## The life of a node

**Announcing.** On start, a node with `WARDEN_UPSTREAM` set posts its name,
address, pool and version to the hub. The first report creates the entry (`201`),
every later one refreshes it (`200`). `first_seen` survives; nothing else does,
so a node that moved to a new address simply reports the new one.

**Renewing.** Three times per `WARDEN_NODE_TTL`, in the background. Reporting
that often means two lost messages still leave a margin before the entry lapses.

**Going stale.** A node that stops reporting keeps its row and is shown as
`stale`. It is not deleted, and that is deliberate: a server that has stopped
answering is a fact worth seeing. Dropping it silently makes it look like it was
never there, which is exactly the wrong impression when a machine has fallen
over.

**Being forgotten.** `warden nodes --forget build-01` removes it, for a machine
that is gone for good. A deliberate act, by a person.

## What survives what

| Situation | What happens |
| --- | --- |
| Hub is down | Nodes hand out ports as usual, log a warning each attempt, and re-announce by themselves once it is back |
| Hub is down at node startup | The node starts anyway; reporting runs in the background and never blocks the boot |
| A node is down | The hub keeps serving, shows that node as `stale` after its lease lapses |
| Node has the wrong cluster token | The hub answers `401`; the node logs it and keeps working locally |
| Node advertises an address the hub cannot reach | Refused at startup, not left to fail quietly later |

The pattern is the same throughout: a node's own work never depends on anything
across the network.

## Two tokens, on purpose

| Variable | Who carries it | What it opens |
| --- | --- | --- |
| `WARDEN_CLUSTER_TOKEN` | wardens, to each other | `POST /v1/nodes` |
| `WARDEN_TOKEN` | a person, or a tool acting for one | everything else, including reading the fleet |

A node has to be able to report without being handed the token that lets a
person read and delete everything. And someone reading the node list is doing
something different from a machine checking in, so the two are not
interchangeable — neither token works in place of the other.

Both are empty by default, which is fine while warden listens on loopback. Set
them before binding to anything else.

## Addresses

`WARDEN_ADVERTISE` is the address the hub should use to reach the node. It
defaults to the node's own listening address, which is right when that address
is already reachable and wrong when the node listens on `0.0.0.0` or `127.0.0.1`.

A node pointing at a hub **elsewhere** while advertising a loopback address is
refused at startup:

```
the warden at http://hub:7010 cannot reach this one at http://127.0.0.1:7020;
set WARDEN_ADVERTISE to an address it can use
```

The alternative is worse than a startup failure: the hub records an address it
can never open, everything looks healthy, and the mistake only surfaces the first
time someone tries to use the entry.

Two wardens on the **same** machine may both use loopback, since a hub there
reaches it perfectly well. That is how to try the whole thing out before
spreading it over two servers.

## Trying it on one machine

```sh
# hub
WARDEN_DATABASE=/tmp/hub.db WARDEN_CLUSTER_TOKEN=secret WARDEN_NODE=hub \
  warden serve --port 7010 --pool 8000-8099

# node
WARDEN_DATABASE=/tmp/edge.db WARDEN_CLUSTER_TOKEN=secret WARDEN_NODE=build-01 \
WARDEN_UPSTREAM=http://127.0.0.1:7010 WARDEN_NODE_TTL=30 \
  warden serve --port 7020 --pool 9000-9099
```

```sh
$ warden nodes --url http://127.0.0.1:7010
NODE      URL                    POOL       VERSION  STATUS  LAST SEEN
build-01  http://127.0.0.1:7020  9000-9099  0.1.0    online  4s ago

$ warden register shop-api --kind backend --url http://127.0.0.1:7020
9000
```

Stop the hub and register something else on the node: it still works. Start the
hub again and the node reports back on its own.

## Settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `WARDEN_NODE` | the machine name | This warden's name in the fleet |
| `WARDEN_UPSTREAM` | empty | Hub to report to; empty means this one is a hub |
| `WARDEN_ADVERTISE` | from host and port | Address the hub should use |
| `WARDEN_CLUSTER_TOKEN` | empty | Shared secret between wardens |
| `WARDEN_NODE_TTL` | `90` | Seconds a node's entry stays fresh |

The machine name is lowercased and stripped of anything a service name would not
accept, so `BUILD-01.office.lan` becomes `build-01.office.lan` rather than
refusing to start.

## Endpoints

| Method | Path | Token | Purpose |
| --- | --- | --- | --- |
| `POST` | `/v1/nodes` | cluster | A warden announces itself; `201` new, `200` renewed |
| `GET` | `/v1/nodes` | API | Every warden this one knows, with status |
| `DELETE` | `/v1/nodes/{name}` | API | Forget a warden |
| `GET` | `/health` | none | Includes `node`, `role` and how many nodes are known |

## What this does not do yet

The hub knows the fleet; it does not yet act on it. Reading across all nodes,
registering through the hub onto a named node, and a fleet view in the dashboard
are tracked in
[#2](https://github.com/vxnsin/warden/issues/2),
[#3](https://github.com/vxnsin/warden/issues/3),
[#4](https://github.com/vxnsin/warden/issues/4),
[#5](https://github.com/vxnsin/warden/issues/5) and
[#6](https://github.com/vxnsin/warden/issues/6).
