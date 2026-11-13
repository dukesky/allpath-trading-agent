#!/bin/sh
# Container entrypoint. `demo` runs the keyless demo in a throwaway folder;
# anything else is an `allpath-trade` command run from /data (the volume).
set -eu

if [ "${1:-serve}" = "demo" ]; then
    # The demo picks a random sign-in token on a non-loopback bind. Inside a
    # container the bind has to be 0.0.0.0, but compose.yaml publishes the
    # port on the host's 127.0.0.1 only, so use the documented `demo` token.
    # The demo keeps a token it finds in its own folder's .env.
    dir="$(mktemp -d)"
    printf "WEB_TOKEN='demo'\n" > "${dir}/.env"
    shift
    exec allpath-trade demo --host 0.0.0.0 --port 8790 --dir "${dir}" "$@"
fi

# First start only: bring in the project folder's .env (mounted read-only at
# /host by compose.yaml) and the agent identity file. After that the copies
# in /data are the ones the app reads and the setup wizard writes.
if [ ! -f /data/.env ] && [ -f /host/.env ]; then
    if cp /host/.env /data/.env 2>/dev/null; then
        chmod 600 /data/.env
        echo "[allpath-docker] imported .env from the project folder into the data volume"
    else
        echo "[allpath-docker] could not read ./.env (file permissions); starting without it." \
             "Use the setup wizard, or: docker compose --profile app cp .env app:/data/.env"
    fi
fi
if [ ! -f /data/IDENTITY.md ]; then
    cp /app/IDENTITY.md /data/IDENTITY.md
fi

if [ "${1:-serve}" = "serve" ]; then
    shift || true
    exec allpath-trade serve --host 0.0.0.0 --port 8791 "$@"
fi
exec allpath-trade "$@"
