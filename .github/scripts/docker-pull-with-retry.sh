#!/usr/bin/env bash
set -uo pipefail

if [ "$#" -ne 1 ] || [ -z "$1" ]; then
  echo "usage: $0 IMAGE" >&2
  exit 64
fi

image="$1"
max_attempts="${DOCKER_PULL_RETRY_ATTEMPTS:-5}"
base_delay="${DOCKER_PULL_RETRY_BASE_SECONDS:-10}"
rate_limit_base_delay="${DOCKER_PULL_RETRY_RATE_LIMIT_SECONDS:-300}"
max_delay="${DOCKER_PULL_RETRY_MAX_SECONDS:-1800}"
jitter_max="${DOCKER_PULL_RETRY_JITTER_SECONDS:-5}"

for value in "$max_attempts" "$base_delay" "$rate_limit_base_delay" "$max_delay" "$jitter_max"; do
  if ! [[ "$value" =~ ^[0-9]+$ ]]; then
    echo "docker pull retry settings must be non-negative integers" >&2
    exit 64
  fi
done
if [ "$max_attempts" -lt 1 ]; then
  echo "DOCKER_PULL_RETRY_ATTEMPTS must be at least 1" >&2
  exit 64
fi

parse_retry_after_seconds() {
  local line value unit whole fraction seconds

  while IFS= read -r line; do
    if [[ "$line" =~ [Rr][Ee][Tt][Rr][Yy][-[:space:]]?[Aa][Ff][Tt][Ee][Rr][^0-9]*([0-9]+([.][0-9]+)?)[[:space:]]*([A-Za-z]+)? ]]; then
      value="${BASH_REMATCH[1]}"
      unit="${BASH_REMATCH[3],,}"
      whole="${value%%.*}"
      fraction=""
      if [[ "$value" == *.* ]]; then
        fraction="${value#*.}"
      fi
      case "$unit" in
        ""|s|sec|secs|second|seconds)
          seconds="$whole"
          if [ -n "$fraction" ]; then
            seconds=$((seconds + 1))
          fi
          printf '%s\n' "$seconds"
          return 0
          ;;
        ms|msec|msecs|millisecond|milliseconds)
          seconds=$(((whole + 999) / 1000))
          if [ "$whole" -eq 0 ] && [ -n "$fraction" ]; then
            seconds=1
          fi
          printf '%s\n' "$seconds"
          return 0
          ;;
        m|min|mins|minute|minutes)
          printf '%s\n' "$((value * 60))"
          return 0
          ;;
        h|hr|hrs|hour|hours)
          printf '%s\n' "$((value * 3600))"
          return 0
          ;;
      esac
    fi
  done < "$log_file"

  printf '0\n'
}

log_file="$(mktemp)"
trap 'rm -f "$log_file"' EXIT

rate_limit_attempt=0

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

  if grep -Eiq 'toomanyrequests|too many requests|(^|[^0-9])429([^0-9]|$)' "$log_file"; then
    rate_limit_attempt=$((rate_limit_attempt + 1))
    delay=$((rate_limit_base_delay * (1 << (rate_limit_attempt - 1))))
    retry_after="$(parse_retry_after_seconds)"
    if [ "$retry_after" -gt "$delay" ]; then
      delay="$retry_after"
    fi
    if [ "$max_delay" -gt 0 ] && [ "$delay" -gt "$max_delay" ]; then
      delay="$max_delay"
    fi
    echo "Registry rate limit detected; retrying in ${delay}s (attempt ${rate_limit_attempt})" >&2
  else
    delay=$((base_delay * (1 << (attempt - 1))))
    if [ "$jitter_max" -gt 0 ]; then
      delay=$((delay + RANDOM % (jitter_max + 1)))
    fi
    if [ "$max_delay" -gt 0 ] && [ "$delay" -gt "$max_delay" ]; then
      delay="$max_delay"
    fi
    echo "Transient registry/network failure; retrying in ${delay}s" >&2
  fi
  sleep "$delay"
done
