#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:5050}"
CREATOR_ID="${CREATOR_ID:-demo-user-1}"
RATE_LIMIT_BURST="${RATE_LIMIT_BURST:-12}"

if ! command -v curl >/dev/null 2>&1; then
  echo "Error: curl is required." >&2
  exit 1
fi

echo "== Provenance Guard Demo Sequence =="
echo "Base URL: ${BASE_URL}"
echo

echo "[1/6] Health check"
curl -s "${BASE_URL}/health"
echo

echo "[2/6] Submit content"
SUBMIT_TEXT="I wrote this entry after a slow walk home from the library and then revised it once before posting. The details are specific to my evening, including the rain on the bus window, a missed crosswalk signal, and the smell of coffee from the corner shop. I kept a few rough phrases on purpose because they sound closer to how I actually think when I am tired and reflective."

SUBMIT_RESPONSE="$(curl -s -X POST "${BASE_URL}/submit" \
  -H "Content-Type: application/json" \
  -H "X-User-Id: ${CREATOR_ID}" \
  -d "{\"text\":\"${SUBMIT_TEXT}\",\"creator_id\":\"${CREATOR_ID}\"}")"

echo "${SUBMIT_RESPONSE}"

CONTENT_ID="$(printf '%s' "${SUBMIT_RESPONSE}" | python -c 'import json,sys; data=json.load(sys.stdin); print(data.get("content_id",""))')"
if [[ -z "${CONTENT_ID}" ]]; then
  echo "Error: Failed to extract content_id from submit response." >&2
  exit 1
fi

echo "Extracted content_id: ${CONTENT_ID}"
echo

echo "[3/6] Content lookup"
curl -s "${BASE_URL}/content/${CONTENT_ID}"
echo

echo "[4/6] Submit appeal"
APPEAL_RESPONSE="$(curl -s -X POST "${BASE_URL}/appeal" \
  -H "Content-Type: application/json" \
  -H "X-User-Id: ${CREATOR_ID}" \
  -d "{\"content_id\":\"${CONTENT_ID}\",\"creator_reasoning\":\"I wrote this myself from personal notes and can provide draft history timestamps if needed.\"}")"

echo "${APPEAL_RESPONSE}"
echo

echo "[5/6] Show latest audit log entries"
curl -s "${BASE_URL}/log"
echo

echo "[6/6] Rate-limit burst (${RATE_LIMIT_BURST} rapid submit requests)"
RATE_TEXT="This is a rate limit test submission intended for automated verification and should be long enough to satisfy validation while being repeated in a short burst for predictable limiter behavior in local testing."

for i in $(seq 1 "${RATE_LIMIT_BURST}"); do
  status_code="$(curl -s -o /dev/null -w "%{http_code}" -X POST "${BASE_URL}/submit" \
    -H "Content-Type: application/json" \
    -d "{\"text\":\"${RATE_TEXT}\",\"creator_id\":\"ratelimit-test\"}")"
  printf 'request %02d -> %s\n' "${i}" "${status_code}"
done

echo
echo "Demo sequence complete."
echo "Tip: if all requests return 200 during rate-limit test, rerun immediately or increase RATE_LIMIT_BURST."
