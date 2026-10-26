#!/usr/bin/env bash
# Read-only check that THIS VM's managed identity can use Azure ML.
# Needs only curl and python3. Prints no token. Changes nothing in Azure.
#
#   bash azure_access_check.sh                      # uses rg-daylight-ml / Daylight-az-ml-workspace
#   bash azure_access_check.sh --sub <subscription-id> --rg <rg> --ws <workspace>
#
# Prerequisites (portal): VM > Identity > System assigned = On, then give that identity
# "AzureML Data Scientist" on the workspace and "Storage Blob Data Contributor" on the
# workspace's storage account. Running it before the roles exist is fine: it shows what is missing.
set -u
SUB=""; RG="${AZUREML_RESOURCE_GROUP:-rg-daylight-ml}"; WS="${AZUREML_WORKSPACE:-Daylight-az-ml-workspace}"
while [ $# -gt 0 ]; do
  case "$1" in --sub) SUB="$2"; shift 2;; --rg) RG="$2"; shift 2;; --ws) WS="$2"; shift 2;;
    *) echo "unknown argument: $1"; exit 2;; esac
done
IMDS="http://169.254.169.254/metadata"
FAILED=0
pass() { printf 'PASS  %s\n' "$1"; }
fail() { printf 'FAIL  %s\n' "$1"; FAILED=1; }
info() { printf '      %s\n' "$1"; }

# json <dotted.path>  reads JSON on stdin, prints the value ('' if missing or not JSON)
json() {
  python3 -c '
import sys, json
try:
    v = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for k in sys.argv[1].split("."):
    v = v.get(k) if isinstance(v, dict) else None
print("" if v is None else v)' "$1"
}
# http <url> <token> [extra curl args]  -> body, then a last line "HTTP <code>"
http() {
  local url="$1" tok="$2"; shift 2
  curl -s --max-time 30 -w '\nHTTP %{http_code}' -H "Authorization: Bearer $tok" "$@" "$url"
}
body_of() { sed '$d'; }
code_of() { tail -n1 | sed 's/^HTTP //'; }

echo "== 1. Managed identity token =="
if [ -z "$SUB" ]; then
  SUB=$(curl -s --max-time 10 -H Metadata:true "$IMDS/instance?api-version=2021-02-01" | json compute.subscriptionId)
  [ -n "$SUB" ] && info "subscription taken from the VM's own metadata: $SUB"
fi
if [ -z "$SUB" ]; then fail "could not read the subscription id; pass --sub"; exit 1; fi
R=$(curl -s --max-time 10 -H Metadata:true "$IMDS/identity/oauth2/token?api-version=2018-02-01&resource=https://management.azure.com/")
T=$(printf '%s' "$R" | json access_token)
if [ -z "$T" ]; then
  fail "no token from the VM identity: $(printf '%s' "$R" | json error_description | cut -c1-200)"
  info "Turn on VM > Identity > System assigned, wait a minute, run again."
  exit 1
fi
CLAIMS=$(T="$T" python3 -c '
import os, json, base64
p = os.environ["T"].split(".")[1]; p += "=" * (-len(p) % 4)
c = json.loads(base64.urlsafe_b64decode(p))
print("object id %s, tenant %s" % (c.get("oid"), c.get("tid")))')
pass "VM identity works ($CLAIMS)"

echo "== 2. Azure ML workspace (needs a role on the workspace) =="
URL="https://management.azure.com/subscriptions/$SUB/resourceGroups/$RG/providers/Microsoft.MachineLearningServices/workspaces/$WS?api-version=2024-04-01"
OUT=$(http "$URL" "$T"); CODE=$(printf '%s' "$OUT" | code_of); BODY=$(printf '%s' "$OUT" | body_of)
LOCATION=""; STORAGE_ID=""
case "$CODE" in
  200) pass "workspace $WS readable"
       LOCATION=$(printf '%s' "$BODY" | json location)
       STORAGE_ID=$(printf '%s' "$BODY" | json properties.storageAccount)
       info "region: $LOCATION" ;;
  403) fail "403 $(printf '%s' "$BODY" | json error.code): the identity has no role on the workspace yet"
       info "Give it 'AzureML Data Scientist' on $WS (IAM > Add role assignment > Managed identity)."
       info "A new role can take up to ~10 minutes to take effect." ;;
  404) fail "404: workspace not found. Check --sub, --rg and --ws" ;;
  *)   fail "HTTP $CODE: $(printf '%s' "$BODY" | head -c 200)" ;;
esac

echo "== 3. Compute cluster (should scale to zero when idle) =="
if [ "$CODE" = "200" ]; then
  OUT=$(http "https://management.azure.com/subscriptions/$SUB/resourceGroups/$RG/providers/Microsoft.MachineLearningServices/workspaces/$WS/computes?api-version=2024-04-01" "$T")
  C2=$(printf '%s' "$OUT" | code_of)
  if [ "$C2" = "200" ]; then
    printf '%s' "$OUT" | body_of | python3 -c '
import sys, json
rows = json.load(sys.stdin).get("value", [])
found = False
for r in rows:
    p = r.get("properties", {}); q = p.get("properties", {}) or {}
    if p.get("computeType") == "AmlCompute":
        s = q.get("scaleSettings", {}) or {}
        found = True
        print("      cluster %s: %s, min %s / max %s nodes, state %s" % (
            r.get("name"), q.get("vmSize"), s.get("minNodeCount"), s.get("maxNodeCount"), q.get("allocationState")))
        if s.get("minNodeCount") not in (0, None):
            print("      WARNING: min nodes is not 0, so it costs money while idle")
if not found:
    print("      no AmlCompute cluster found")'
    pass "compute listing works"
  else
    fail "compute list returned HTTP $C2"
  fi
else
  info "skipped (workspace not readable)"
fi

echo "== 4. Workspace storage (needed to upload training data) =="
if [ -n "$STORAGE_ID" ]; then
  ACCT=${STORAGE_ID##*/}
  ST=$(curl -s --max-time 10 -H Metadata:true "$IMDS/identity/oauth2/token?api-version=2018-02-01&resource=https://storage.azure.com/" | json access_token)
  if [ -z "$ST" ]; then fail "no storage token"; else
    OUT=$(http "https://$ACCT.blob.core.windows.net/?comp=list&maxresults=5" "$ST" -H "x-ms-version: 2021-08-06")
    C3=$(printf '%s' "$OUT" | code_of); B3=$(printf '%s' "$OUT" | body_of)
    case "$C3" in
      200) pass "storage account $ACCT reachable with this identity" ;;
      403) ERR=$(printf '%s' "$B3" | grep -o '<Code>[^<]*' | head -1 | sed 's/<Code>//')
           fail "403 $ERR on $ACCT"
           info "AuthorizationPermissionMismatch: give the identity 'Storage Blob Data Contributor' on this storage account."
           info "AuthorizationFailure: the storage firewall is blocking this VM's IP." ;;
      *)   fail "HTTP $C3 from $ACCT" ;;
    esac
  fi
else
  info "skipped (storage account unknown until the workspace is readable)"
fi

echo "== 5. Azure ML endpoints reachable from this VM =="
for H in management.azure.com ml.azure.com ${LOCATION:+$LOCATION.api.azureml.ms}; do
  C=$(curl -s -o /dev/null --max-time 10 -w '%{http_code}' "https://$H/")
  if [ "$C" = "000" ]; then fail "cannot reach $H (outbound blocked?)"; else pass "$H reachable (HTTP $C)"; fi
done

echo
if [ "$FAILED" = "0" ]; then echo "ALL CHECKS PASSED"; else echo "SOME CHECKS FAILED: see FAIL lines above"; fi
exit "$FAILED"
