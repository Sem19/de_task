#!/bin/sh
# One scheduled extraction. Invoked by supercronic; not used by make extract.
set -eu
root="$(CDPATH= cd -- "$(dirname "$0")" && pwd)"
cd "$root"

if command -v flock >/dev/null 2>&1; then
  exec 9>/tmp/extractor.schedule.lock
  if ! flock -n 9; then
    echo "scheduler: skipped, previous extraction still running" >&2
    exit 0
  fi
fi

echo "scheduler: starting python main.py"
set +e
python main.py
status=$?
set -e

if [ "$status" -ne 0 ]; then
  echo "scheduler: python main.py exited $status" >&2
fi
exit "$status"
