#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
    cat <<'EOF'
Usage: ssh_keepalive.sh [ssh options] destination [remote command ...]

The wrapper sends SSH keepalive probes and retries a dropped connection. Set
SSH_KEEPALIVE_MAX_RETRIES=0 for unlimited retries. A successful remote command
exits normally; authentication failures are retried only up to the limit.
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" || "$#" -eq 0 ]]; then
    usage
    [[ "$#" -eq 0 ]] && exit 2
    exit 0
fi

interval="${SSH_KEEPALIVE_INTERVAL:-20}"
count="${SSH_KEEPALIVE_COUNT:-6}"
connect_timeout="${SSH_CONNECT_TIMEOUT:-15}"
retry_delay="${SSH_RECONNECT_DELAY:-3}"
max_retries="${SSH_KEEPALIVE_MAX_RETRIES:-3}"

if [[ ! "$interval" =~ ^[1-9][0-9]*$ || ! "$count" =~ ^[1-9][0-9]*$ || ! "$connect_timeout" =~ ^[1-9][0-9]*$ || ! "$retry_delay" =~ ^[0-9]+$ || ! "$max_retries" =~ ^[0-9]+$ ]]; then
    printf 'SSH keepalive settings must be non-negative integers; interval, count and timeout must be positive.\n' >&2
    exit 2
fi

ssh_args=(
    -o "ServerAliveInterval=${interval}"
    -o "ServerAliveCountMax=${count}"
    -o TCPKeepAlive=yes
    -o "ConnectTimeout=${connect_timeout}"
    -o ConnectionAttempts=1
)

while [[ "$#" -gt 0 ]]; do
    case "$1" in
        --)
            shift
            break
            ;;
        -p|-i|-F|-J|-l|-o)
            option="$1"
            ssh_args+=("$option")
            shift
            if [[ "$#" -eq 0 ]]; then
                printf 'SSH option %s requires a value.\n' "$option" >&2
                exit 2
            fi
            ssh_args+=("$1")
            shift
            ;;
        -*)
            ssh_args+=("$1")
            shift
            ;;
        *)
            break
            ;;
    esac
done

if [[ "$#" -eq 0 ]]; then
    usage
    exit 2
fi
destination="$1"
shift

attempt=0
while :; do
    set +e
    ssh "${ssh_args[@]}" "$destination" "$@"
    status=$?
    set -e

    if [[ "$status" -eq 0 ]]; then
        exit 0
    fi
    attempt=$((attempt + 1))
    if [[ "$max_retries" -ne 0 && "$attempt" -gt "$max_retries" ]]; then
        printf 'SSH connection ended with status %s after %s attempt(s).\n' "$status" "$((attempt))" >&2
        exit "$status"
    fi
    printf 'SSH connection ended with status %s; retrying in %s seconds.\n' "$status" "$retry_delay" >&2
    sleep "$retry_delay"
done
