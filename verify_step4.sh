#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  bash verify_step4.sh --image <docker-image> [options]

Required:
  --image <docker-image>        Docker image for the test function (e.g. user/bfs)

Options:
  --name <function-name>        OpenFaaS function name (default: step4test-<timestamp>)
  --requests <n>                Number of requests to send (default: 120)
  --delay-ms <ms>               Delay between requests in ms (default: 200)
  --rate <n>                    Eviction rate value in ENV tuple (default: 20)
  --strategy <value>            Strategy string (default incremental strategy)
  --keep-function               Do not remove function after test
  --skip-log-check              Skip pod-log dump command check
  -h, --help                    Show this help

Example:
  bash verify_step4.sh \
    --image pronghornae/bfs \
    --name bfs-step4-test
EOF
}

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "[Error] Missing required command: $1"
    exit 1
  fi
}

IMAGE=""
FUNCTION_NAME="step4test-$(date +%s)"
REQUESTS=120
DELAY_MS=200
RATE=20
STRATEGY="request_centric&max_capacity=12&incremental=true&max_chain_depth=5"
KEEP_FUNCTION=0
CHECK_LOGS=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --image)
      IMAGE="$2"
      shift 2
      ;;
    --name)
      FUNCTION_NAME="$2"
      shift 2
      ;;
    --requests)
      REQUESTS="$2"
      shift 2
      ;;
    --delay-ms)
      DELAY_MS="$2"
      shift 2
      ;;
    --rate)
      RATE="$2"
      shift 2
      ;;
    --strategy)
      STRATEGY="$2"
      shift 2
      ;;
    --keep-function)
      KEEP_FUNCTION=1
      shift
      ;;
    --skip-log-check)
      CHECK_LOGS=0
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "[Error] Unknown argument: $1"
      usage
      exit 1
      ;;
  esac
done

if [[ -z "$IMAGE" ]]; then
  echo "[Error] --image is required."
  usage
  exit 1
fi

require_cmd faas-cli
require_cmd kubectl
require_cmd curl
require_cmd python3

PF_PID=""
TMP_STATE="$(mktemp)"
TMP_PARSE_OUT="$(mktemp)"

cleanup() {
  if [[ -n "$PF_PID" ]] && kill -0 "$PF_PID" >/dev/null 2>&1; then
    kill "$PF_PID" >/dev/null 2>&1 || true
  fi

  rm -f "$TMP_STATE" "$TMP_PARSE_OUT"

  if [[ "$KEEP_FUNCTION" -eq 0 ]]; then
    faas-cli remove "$FUNCTION_NAME" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

echo "[Info] Deploying function: $FUNCTION_NAME"
faas-cli remove "$FUNCTION_NAME" >/dev/null 2>&1 || true
faas-cli deploy --image="$IMAGE" --name="$FUNCTION_NAME" --env "ENV=${STRATEGY},true,${RATE}"

echo "[Info] Waiting for function pod to become ready..."
kubectl wait \
  --for=condition=ready \
  pod \
  -l "faas_function=${FUNCTION_NAME}" \
  -n openfaas-fn \
  --timeout=300s

DELAY_SEC="$(python3 -c "print(${DELAY_MS}/1000.0)")"

echo "[Info] Sending $REQUESTS requests to trigger checkpoints..."
for ((i=1; i<=REQUESTS; i++)); do
  # Retry each request briefly to tolerate transient startup/rollout timing.
  ok=0
  for _ in 1 2 3; do
    if curl -fsS "http://127.0.0.1:8080/function/${FUNCTION_NAME}?mutability=1" >/dev/null; then
      ok=1
      break
    fi
    sleep 1
  done
  if [[ "$ok" -ne 1 ]]; then
    echo "[Error] Request $i failed after retries."
    exit 1
  fi
  sleep "$DELAY_SEC"
done

echo "[Info] Reading orchestrator state from database service..."
kubectl port-forward -n stores svc/database-svc 5000:5000 >/tmp/verify-step4-pf.log 2>&1 &
PF_PID=$!
sleep 2

read_ok=0
for _ in 1 2 3 4 5; do
  if curl -fsS "http://127.0.0.1:5000/read/${FUNCTION_NAME}?next_expected_id=-1" > "$TMP_STATE"; then
    read_ok=1
    break
  fi
  sleep 1
done
if [[ "$read_ok" -ne 1 ]]; then
  echo "[Error] Could not read orchestrator state for function: $FUNCTION_NAME"
  exit 1
fi

python3 - "$TMP_STATE" "$TMP_PARSE_OUT" <<'PY'
import json
import sys

state_path = sys.argv[1]
out_path = sys.argv[2]

with open(state_path, "r", encoding="utf-8") as f:
    outer = json.load(f)

raw_data = outer.get("data")
if not raw_data:
    raise SystemExit("[Error] Empty orchestrator data payload.")

state = json.loads(raw_data)
strategy = state.get("strategy")
if isinstance(strategy, str):
    strategy = json.loads(strategy)

pool = strategy.get("pool", [])
pool = sorted(pool, key=lambda c: c["state"]["request_number"])

print("[Info] Checkpoints in pool:", len(pool))
for chk in pool:
    req = chk["state"]["request_number"]
    path = chk["path"]
    parent = chk.get("parent_path")
    print(f"  req={req:>3}  path={path}  parent_path={parent}")

if len(pool) < 2:
    raise SystemExit("[Error] Need at least 2 checkpoints for Step 4 verification.")

paths = {c["path"] for c in pool}
children = [c for c in pool if c.get("parent_path") is not None]
if not children:
    raise SystemExit("[Error] No incremental checkpoints found (all parent_path are None).")

for child in children:
    if child["parent_path"] not in paths:
        raise SystemExit(
            f"[Error] Broken chain: parent_path '{child['parent_path']}' not found in pool."
        )

first = pool[0]
second = pool[1]
if first.get("parent_path") is not None:
    raise SystemExit("[Error] First checkpoint should be full dump with parent_path=None.")
if second.get("parent_path") != first["path"]:
    raise SystemExit(
        "[Error] Second checkpoint is expected to reference first checkpoint as parent_path."
    )

with open(out_path, "w", encoding="utf-8") as f:
    json.dump({"first_path": first["path"], "second_path": second["path"]}, f)

print("[OK] Chain metadata check passed: first full, second incremental.")
PY

if [[ "$CHECK_LOGS" -eq 1 ]]; then
  POD_NAME="$(kubectl get pods -n openfaas-fn -l "faas_function=${FUNCTION_NAME}" -o jsonpath='{.items[0].metadata.name}')"
  LOG_LINES="$(kubectl logs -n openfaas-fn "$POD_NAME" --all-containers=true 2>/dev/null | grep 'DUMP_CMD:' || true)"

  if [[ -z "$LOG_LINES" ]]; then
    echo "[Warn] No DUMP_CMD lines found in pod logs."
    echo "[Warn] Add 'print(f\"DUMP_CMD: {dump_cmd}\")' before os.system(dump_cmd) in agent-python/main.py to verify --prev-images-dir directly."
  else
    FIRST_DUMP_LINE="$(echo "$LOG_LINES" | sed -n '1p')"
    SECOND_DUMP_LINE="$(echo "$LOG_LINES" | sed -n '2p')"

    echo "[Info] First dump line:  $FIRST_DUMP_LINE"
    echo "[Info] Second dump line: $SECOND_DUMP_LINE"

    if echo "$FIRST_DUMP_LINE" | grep -q -- '--prev-images-dir'; then
      echo "[Error] First dump unexpectedly includes --prev-images-dir."
      exit 1
    fi

    if ! echo "$SECOND_DUMP_LINE" | grep -q -- '--prev-images-dir'; then
      echo "[Error] Second dump does not include --prev-images-dir."
      exit 1
    fi

    echo "[OK] Dump command check passed: second dump uses --prev-images-dir."
  fi
fi

echo "[Done] Step 4 verification succeeded for function: $FUNCTION_NAME"
