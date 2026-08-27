#!/bin/sh
set -eu
if [ "$#" -ne 1 ]; then
  exit 2
fi
fixture_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
destination=$1
mkdir -p "$destination"
if [ -n "$(find "$destination" -mindepth 1 -print -quit)" ]; then
  exit 3
fi
cp "$fixture_dir/base/app.py" "$destination/app.py"
git -C "$destination" init -b main >/dev/null
git -C "$destination" add app.py
git -C "$destination" -c user.name=synthetic -c user.email=synthetic@example.invalid commit -m base >/dev/null
cp "$fixture_dir/head/app.py" "$destination/app.py"
git -C "$destination" add app.py
git -C "$destination" -c user.name=synthetic -c user.email=synthetic@example.invalid commit -m change >/dev/null
