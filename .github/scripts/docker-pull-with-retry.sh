#!/usr/bin/env bash
set -uo pipefail

if [ "$#" -ne 1 ] || [ -z "$1" ]; then
  echo "usage: $0 IMAGE" >&2
  exit 64
fi

image="$1"
max_attempts="${DOCKER_PULL_RETRY_ATTEMPTS:-5}"
base_delay="${DOCKER_PULL_RETRY_BASE_SECONDS:-10}"
jitter_max="${DOCKER_PULL_RETRY_JITTER_SECONDS:-5}"

for value in "$max_attempts" "$base_delay" "$jitter_max"; do
  if ! [[ "$value" =~ ^[0-9]+$ ]]; then
    echo "docker pull retry settings must be non-negative integers" >&2
    exit 64
  fi
done
if [ "$max_attempts" -lt 1 ]; then
  echo "DOCKER_PULL_RETRY_ATTEMPTS must be at least 1" >&2
  exit 64
fi

log_file="$(mktemp)"
trap 'rm -f "$log_file"' EXIT

for ((attempt = 1; attempt <= max_attempts; attempt++)); do
  : >"$log_file"
  echo "Pulling ${image} (attempt ${attempt}/${max_attempts})"
  if docker pull "$image" 2>&1 | tee "$log_file"; then
    exit 0
  else
    status="${PIPESTATUS[0]}"
  fi

  if [ "$attempt" -ge "$max_attempts" ]; then
    echo "docker pull exhausted ${max_attempts} attempts" >&2
    exit "$status"
  fi

  if ! grep -Eiq \
    'toomanyrequests|too many requests|(^|[^0-9])429([^0-9]|$)|retry-after|timed? ?out|timeout|connection (reset|refused|aborted)|temporary failure|temporarily unavailable|unexpected eof|tls handshake timeout|(^|[^0-9])50[0-4]([^0-9]|$)|bad gateway|service unavailable|gateway timeout|internal server error' \
    "$log_file"; then
    echo "docker pull failed with a non-transient error; not retrying" >&2
    exit "$status"
  fi

  delay=$((base_delay * (1 << (attempt - 1))))
  if [ "$jitter_max" -gt 0 ]; then
    delay=$((delay + RANDOM % (jitter_max + 1)))
  fi
  echo "Transient registry/network failure; retrying in ${delay}s" >&2
  sleep "$delay"
done
