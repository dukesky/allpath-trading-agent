#!/usr/bin/env bash
# Smoke test for `allpath-trade demo` with no LLM key: start it on a free
# loopback port, wait for /login, sign in, load the chat page, ask one
# scripted question, stop it. Used by CI (.github/workflows/ci.yml) and
# runnable locally: scripts/demo-smoke.sh
set -euo pipefail

unset OPENROUTER_API_KEY OPENAI_API_KEY ANTHROPIC_API_KEY LLM_PROVIDER

port="${DEMO_SMOKE_PORT:-$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')}"
work="$(mktemp -d)"
base="http://127.0.0.1:${port}"
jar="${work}/cookies.txt"

uv run allpath-trade demo --host 127.0.0.1 --port "${port}" --dir "${work}/demo" \
  >"${work}/demo.log" 2>&1 &
pid=$!
cleanup() {
  kill "${pid}" 2>/dev/null || true
  wait "${pid}" 2>/dev/null || true
  rm -rf "${work}"
}
trap cleanup EXIT

fail() {
  echo "demo smoke test FAILED: $1" >&2
  echo "--- demo log ---" >&2
  cat "${work}/demo.log" >&2 || true
  exit 1
}

for _ in $(seq 1 90); do
  kill -0 "${pid}" 2>/dev/null || fail "the demo process exited early"
  code="$(curl -s -o /dev/null -w '%{http_code}' "${base}/login" || true)"
  [ "${code}" = "200" ] && break
  sleep 1
done
[ "${code}" = "200" ] || fail "/login did not answer 200 within 90 s (last: ${code})"
echo "ok: /login 200 on ${base}"

curl -s -o /dev/null -c "${jar}" --data "token=demo" "${base}/login"

chat="$(curl -s -b "${jar}" -w '\n%{http_code}' "${base}/chat")"
[ "$(tail -n 1 <<<"${chat}")" = "200" ] || fail "/chat did not answer 200"
grep -q 'id="demo-prompts"' <<<"${chat}" || fail "/chat has no keyless demo prompts"
grep -q 'Chat needs an LLM key' <<<"${chat}" && fail "/chat shows the no-key notice"
echo "ok: /chat 200 with the keyless demo prompts"

reply="$(curl -s -b "${jar}" -F "message=What's my riskiest position?" "${base}/chat/send")"
grep -q 'riskiest position' <<<"${reply}" || fail "the scripted chat did not answer"
grep -q 'get_portfolio' <<<"${reply}" || fail "the scripted answer did not use the tools"
echo "ok: scripted chat answered through the tool loop"

echo "demo smoke test passed"
