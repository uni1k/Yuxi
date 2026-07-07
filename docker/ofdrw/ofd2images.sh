#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -ne 2 ]]; then
  echo "usage: yuxi-ofdrw-ofd2images <input.ofd> <output-dir>" >&2
  exit 2
fi

exec java -cp "/app/tools/ofdrw/ofdrw-runner.jar:/app/tools/ofdrw/lib/*" io.yuxi.tools.OfdrwImageExporter "$1" "$2"
