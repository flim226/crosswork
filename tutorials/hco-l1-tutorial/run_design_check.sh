#!/usr/bin/env bash
# Run verify_l1_plan.py against a plan file as a Crosswork Planning Design batch job.
# Usage: run_design_check.sh <plan.txt> <outdir> [host:port ...]
# Needs GW (https://<cp-host>:30603/cp/apiGateway) and TOKEN (a Crosswork JWT) in the environment.
# curl verifies TLS with CURL_CA_BUNDLE; extra curl options can be given in CURL_OPTS.
set -euo pipefail
PLAN=$1; OUT=$2; shift 2
: "${GW:?set GW}" "${TOKEN:?set TOKEN}"
DIR=$(cd "$(dirname "$0")" && pwd)
H=$(mktemp); chmod 600 "$H"; trap 'rm -f "$H"' EXIT
printf 'Authorization: Bearer %s\n' "$TOKEN" > "$H"
api() { curl -sSf ${CURL_OPTS:-} -H @"$H" "$@"; }
field() { python3 -c 'import json,sys; print(json.loads(json.loads(sys.argv[1])["data"]["additionalInfo"])[sys.argv[2]])' "$1" "$2"; }

P=$(api -F "file=@$PLAN;type=application/octet-stream" -F "fileName=$(basename "$PLAN")" \
      -F category=PLANFILE -F tags=l1-check "$GW/design/v1/file/import")
S=$(api -F "file=@$DIR/verify_l1_plan.py;type=application/octet-stream" -F "fileName=verify_l1_plan.py" \
      -F category=SCRIPT -F tags=l1-check "$GW/design/v1/file/import")
PN=$(field "$P" fileName); SN=$(field "$S" fileName)
FS=$(api "$GW/design/v1/jobs/input?fileId=$(field "$P" fileId)&fileId=$(field "$S" fileId)" \
      | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"]["fsId"])')
BODY=$(python3 -c 'import json,sys; print(json.dumps({"jobName":"L1 plan check","jobType":"USER_SCRIPT","fsId":sys.argv[1],
  "jobConfig":json.dumps({"scriptType":"PYTHON","options":" ".join(sys.argv[2:])}),"inPlanFileId":sys.argv[3],
  "requestedProfile":"4000m vCPU, 16Gi Mem","jobPriority":"HIGH"}))' "$FS" "$SN" "$PN" "$@")
JOB=$(api -H 'Content-Type: application/json' -X POST "$GW/design/v1/jobs" -d "$BODY" \
      | python3 -c 'import json,sys,re; print(re.search(r"(\d+)$", json.load(sys.stdin)["data"]["message"]).group(1))')
echo "Submitted job $JOB (plan $PN, script $SN)"
for _ in $(seq 1 60); do
  R=$(api -H 'Content-Type: application/json' -X POST "$GW/design/v1/jobs/search?page=0&size=1" \
        -d "{\"filters\":[{\"condition\":{\"column\":\"jobId\",\"operator\":\"EQUALS\",\"value\":\"$JOB\"},\"conjuction\":\"AND\"}]}")
  ST=$(python3 -c 'import json,sys; c=json.loads(sys.argv[1])["data"]["content"][0]; print(c.get("jobStatus") or c.get("status"))' "$R")
  case $ST in COMPLETED|FAILED|ABORTED) break;; esac
  sleep 10
done
echo "Job status: $ST"
TID=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["data"]["content"][0].get("outputTarFileId") or "")' "$R")
[ -n "$TID" ] || { python3 -c 'import json,sys; print(json.loads(sys.argv[1])["data"]["content"][0].get("jobResult"))' "$R"; exit 1; }
mkdir -p "$OUT"
api -H 'Accept: */*' -o "$OUT/job.tar" "$GW/design/v1/file/export?fileId=$TID"
tar -xf "$OUT/job.tar" -C "$OUT"
echo "Script output: $OUT/stdoutLog.txt"
