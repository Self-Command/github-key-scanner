#!/usr/bin/env python3
"""Reload the existing system Nginx ingress despite a shared PID file."""
import json
import os
import re
import signal
import subprocess
from pathlib import Path

subprocess.run(["/usr/sbin/nginx", "-t"], check=True)
listeners = subprocess.check_output(["ss", "-lntp"], text=True)
pids = set()
for line in listeners.splitlines():
    columns = line.split()
    if len(columns) > 3 and columns[3].endswith(":443"):
        pids.update(int(pid) for pid in re.findall(r"pid=(\d+)", line))
candidates = []
for pid in pids:
    path = Path("/proc") / str(pid)
    try:
        command = (path / "cmdline").read_bytes().replace(b"\0", b" ").decode().strip()
        if (
            command.startswith("nginx: master process /usr/sbin/nginx ")
            and (" -c " not in command or " -c /etc/nginx/nginx.conf" in command)
            and os.readlink(path / "exe") == "/usr/sbin/nginx"
        ):
            candidates.append(pid)
    except OSError:
        continue
if len(candidates) != 1:
    raise SystemExit("Expected exactly one existing system HTTPS ingress; refusing ambiguous reload")
os.kill(candidates[0], signal.SIGHUP)
print(json.dumps({"nginx_https_master_reloaded": candidates[0]}))
