# Updates

warden can tell you when a newer version exists, and a hub can ask every machine
in a fleet to go and fetch it.

## Is there a newer one?

```sh
$ warden update
warden 0.2.0 is out, this is 0.1.0
https://github.com/vxnsin/warden/releases/tag/v0.2.0
```

A running warden asks GitHub for the newest release every six hours and keeps
the answer in memory, so the command returns instantly and nothing waits on the
network. If it has no answer it says why rather than guessing:

```
0.1.0; vxnsin/warden has published no releases yet
```

Nothing is ever compared to a version that is not a version: a tag like
`nightly` is ignored, and a pre-release is not treated as newer than the release
it precedes.

Turn the check off entirely with `WARDEN_UPDATE_CHECK=false`. It is the only
thing warden sends anywhere, it fails silently when the machine is offline, and
it never blocks a command.

## Updating one machine

```sh
$ warden update --apply
Update the warden at http://127.0.0.1:7010? [y/N]: y
Successfully installed warden-0.2.0
```

This runs the update command **that machine** is configured with, and nothing
else.

## Updating a fleet

From the hub:

```sh
$ warden update --fleet --yes --url http://hub:7010
NODE      RESULT   DETAIL
build-01  updated  Successfully installed warden-0.2.0
db-03     refused  updating over the API is switched off - set
                   WARDEN_ALLOW_REMOTE_UPDATE=true on this warden to allow it
hub       updated  Successfully installed warden-0.2.0
web-02    updated  Successfully installed warden-0.2.0
```

Every node is asked and every answer is reported. One machine refusing, or being
unreachable, does not stop the others and does not fail the command — you get a
row per node saying what happened to it.

## The hub sends an intent, never a command

This is the part worth being careful about, so it is worth stating plainly.

`POST /v1/update` means **"update yourself"**. It carries no payload. What
updating means on a given machine comes from that machine's own
`WARDEN_UPDATE_COMMAND` and from nowhere else.

The alternative — letting the hub send a command to run — would make warden a
way to execute anything, anywhere in the fleet. One leaked cluster token would
then be worth every machine it can reach. As it stands, the worst a leaked token
can do is ask machines to run the update they were already configured to run.

Two switches have to be on before anything happens at all, and both live on the
machine being asked:

| Setting | Without it |
| --- | --- |
| `WARDEN_ALLOW_REMOTE_UPDATE=true` | `403`, "updating over the API is switched off" |
| `WARDEN_UPDATE_COMMAND` | `403`, "does not know how to update itself" |

Both default to off. A warden you have not deliberately configured for this will
refuse, and say so.

## Choosing the command

It runs without a shell, so it is one program with arguments rather than a
pipeline. Put anything longer in a script and point at the script.

**From a checkout, installed as a tool:**

```sh
WARDEN_UPDATE_COMMAND="/usr/local/bin/update-warden.sh"
```

```sh
#!/bin/sh
set -e
cd /opt/warden
git pull --ff-only
uv tool install . --force
systemctl --user restart warden
```

**The restart is your job.** warden does not restart itself: it cannot report
the result of a command that ends the process running it. Whatever you configure
should finish by restarting the service, and the row you get back is the output
of the command up to that point.

Note also that the machine has to be able to reach wherever it pulls from. A
node that can talk to the hub but not to your git host will refuse with whatever
git said, which is the point of passing the output back.

## Settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `WARDEN_UPDATE_CHECK` | `true` | Ask GitHub whether a newer release exists |
| `WARDEN_UPDATE_REPO` | `vxnsin/warden` | Which repository to ask about |
| `WARDEN_UPDATE_INTERVAL` | `21600` | Seconds between checks, at least 300 |
| `WARDEN_ALLOW_REMOTE_UPDATE` | `false` | Let a caller ask this warden to update itself |
| `WARDEN_UPDATE_COMMAND` | empty | What updating means on this machine |

## Endpoints

| Method | Path | Token | Purpose |
| --- | --- | --- | --- |
| `GET` | `/v1/update` | either | Whether a newer warden exists |
| `POST` | `/v1/update` | either | Ask this warden to update itself |
| `POST` | `/v1/fleet/update` | API | Ask every warden in the fleet to update itself |

"Either" means the API token or the cluster token: a hub does this on its rounds,
an operator does it by hand, and `WARDEN_ALLOW_REMOTE_UPDATE` is the gate that
actually decides.
