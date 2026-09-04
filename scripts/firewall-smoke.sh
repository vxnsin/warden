#!/usr/bin/env bash
# Everything the firewall does to a real machine, done to a real machine.
#
# Runs inside a container with its own network namespace and NET_ADMIN, so a
# `policy drop` here cannot reach whatever is running it.
set -euo pipefail

export WARDEN_DATABASE=/tmp/firewall-smoke.db
export WARDEN_CONFIG=/tmp/firewall-smoke.toml
rm -f "$WARDEN_DATABASE"

say() { printf '\n=== %s ===\n' "$1"; }

say "nft is here and the ruleset starts empty"
nft list ruleset
test -z "$(nft list ruleset)"

say "a rule, applied for real"
warden firewall allow ssh --from 10.0.0.0/8 > /dev/null
warden firewall apply --yes --rollback 0
nft list ruleset | tee /tmp/applied.txt
grep -q 'tcp dport 22 accept' /tmp/applied.txt
grep -q 'ct state established,related accept' /tmp/applied.txt

say "restore puts back what was there before"
warden firewall restore
test -z "$(nft list ruleset)"

say "nobody confirms, so it rolls itself back"
warden firewall allow 8080 > /dev/null
warden firewall apply --yes --rollback 3
nft list ruleset | grep -q 'tcp dport 8080'
sleep 8
if nft list ruleset | grep -q 'tcp dport 8080'; then
  echo "the rollback never happened - this is the failure that loses servers" >&2
  nft list ruleset >&2
  exit 1
fi
echo "went back on its own"

say "confirming keeps it"
warden firewall apply --yes --rollback 3
warden firewall confirm
sleep 8
nft list ruleset | grep -q 'tcp dport 8080'
echo "still there, as asked"

say "a refused apply leaves nothing armed"
warden firewall status --json | grep -q '"rollback_at": null'

echo
echo "all of it, on a real ruleset"
