#!/usr/bin/env python3
from pathlib import Path
import json
import os
import sys


path = os.environ.get("FAKE_DOCKER_LOG")
if path:
  with Path(path).open("a", encoding="utf-8") as output:
    output.write(json.dumps(sys.argv[1:]) + "\n")
raise SystemExit(int(os.environ.get("FAKE_DOCKER_EXIT", "0")))
