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
nft list ruleset > /tmp/now.txt; grep -q 'tcp dport 8080' /tmp/now.txt
sleep 8
nft list ruleset > /tmp/now.txt
if grep -q 'tcp dport 8080' /tmp/now.txt; then
  echo "the rollback never happened - this is the failure that loses servers" >&2
  nft list ruleset >&2
  exit 1
fi
echo "went back on its own"

say "confirming keeps it"
warden firewall apply --yes --rollback 3
warden firewall confirm
sleep 8
nft list ruleset > /tmp/now.txt; grep -q 'tcp dport 8080' /tmp/now.txt
echo "still there, as asked"

say "a refused apply leaves nothing armed"
warden firewall status --json > /tmp/status.json
grep -q '"rollback_at": null' /tmp/status.json


say "taking over from a real ufw"
export DEBIAN_FRONTEND=noninteractive
apt-get -qq install -y --no-install-recommends ufw > /dev/null 2>&1 || {
  echo "no ufw available here, skipping the adoption round"; exit 0;
}
ufw --force reset > /dev/null 2>&1 || true
ufw allow 22/tcp > /dev/null
ufw allow from 10.0.0.0/8 to any port 8000:8100 proto tcp > /dev/null
ufw deny 3389 > /dev/null
ufw --force enable > /dev/null 2>&1 || {
  echo "ufw would not start in this container, reading it anyway"; }

echo "what ufw says it holds:"
ufw status numbered || true

rm -f "$WARDEN_DATABASE"
warden firewall adopt --manager ufw --yes --rollback 0
warden firewall list

say "every ufw rule came across"
warden firewall list --json > /tmp/adopted.json
python - <<'PY'
import json, sys
rules = json.load(open("/tmp/adopted.json"))
ports = {tuple(sorted(r["ports"])) for r in rules}
missing = [w for w in ((22,), (3389,)) if w not in ports]
ranged = [r for r in rules if len(r["ports"]) == 101 and r["source"] == "10.0.0.0/8"]
if missing or not ranged:
    print("lost something in the crossing:", missing, "range kept:", bool(ranged), file=sys.stderr)
    print(json.dumps(rules, indent=2), file=sys.stderr)
    sys.exit(1)
print("22, 3389 and 8000:8100 from 10.0.0.0/8 all arrived")
PY

say "and it is loaded, while ufw is still enabled"
nft list ruleset > /tmp/now.txt; grep -q 'tcp dport 22' /tmp/now.txt
echo "warden holds the ruleset; ufw goes only on confirm"
