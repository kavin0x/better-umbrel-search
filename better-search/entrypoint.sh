#!/bin/sh
# Umbrel creates the data directory as root. 0.3.x also wrote the index as root.
# Stay root only long enough to hand that directory to the app user, then listen as uid 1000.
set -eu
data_dir="${DATA_PATH:-/data}"
if [ -z "$data_dir" ] || [ "$data_dir" = "/" ]; then
  echo "DATA_PATH must be a directory other than /" >&2
  exit 1
fi
case "$data_dir" in
  /*) ;;
  *)
    echo "DATA_PATH must be absolute" >&2
    exit 1
    ;;
esac
if [ "$(id -u)" = "0" ]; then
  mkdir -p "$data_dir"
  app_uid=$(id -u app)
  owner=$(stat -c %u "$data_dir")
  sqlite_owner=$owner
  if [ -e "$data_dir/index.sqlite" ]; then
    sqlite_owner=$(stat -c %u "$data_dir/index.sqlite")
  fi
  # A recursive chown of the model cache blocks the port. Do it once, when
  # the folder or the index is still owned by root.
  if [ "$owner" != "$app_uid" ] || [ "$sqlite_owner" != "$app_uid" ]; then
    chown -R app:app "$data_dir"
  fi
  exec setpriv --reuid=app --regid=app --clear-groups -- "$@"
fi
exec "$@"
