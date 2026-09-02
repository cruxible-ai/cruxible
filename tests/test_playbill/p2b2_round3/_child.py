"""Adversarial provider child used by the round-3 descendant/fence probes."""

from __future__ import annotations

from pathlib import Path

SOURCE = r'''#!/usr/bin/env python3
import json, os, socket, subprocess, sys, threading, time

MODE = "@MODE@"
MARKER = "@MARKER@"

invocation_id, control_path = sys.argv[2:4]
server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
server.bind(control_path)
os.chmod(control_path, 0o600)
server.listen(2)

def echo():
    while True:
        try:
            connection, _ = server.accept()
        except OSError:
            return
        with connection:
            received = connection.recv(4096).decode("utf-8")
            answer = invocation_id.encode("utf-8") if received == invocation_id else b""
            connection.sendall(answer)

threading.Thread(target=echo, daemon=True).start()
document = json.loads(sys.stdin.buffer.read())

LOOP = (
    "import sys,time\n"
    "path=sys.argv[1]\n"
    "while True:\n"
    "    open(path,'a').write('x')\n"
    "    time.sleep(0.02)\n"
)

pass_fds = ()
argument = MARKER
body = LOOP
preexec = None
wait_for_marker = True

if MODE == "leak":
    fd = document["secret_channel"]["fd"]
    pass_fds = (fd,)
    body = (
        "import os,sys,time\n"
        "time.sleep(1.5)\n"
        "data = os.read(" + str(fd) + ", 65536)\n"
        "open(sys.argv[1],'wb').write(data)\n"
        "time.sleep(30)\n"
    )
    wait_for_marker = False
elif MODE == "setpgid":
    preexec = lambda: os.setpgid(0, 0)

descendant = subprocess.Popen(
    [sys.executable, "-c", body, argument],
    pass_fds=pass_fds,
    preexec_fn=preexec,
    close_fds=True,
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
if wait_for_marker:
    for _ in range(400):
        if os.path.exists(MARKER):
            break
        time.sleep(0.01)

envelope = {
    "protocol_version": "1.0",
    "run_id": document["run_id"],
    "status": "ok",
    "output": {"echo": document["input"]["value"]},
    "refusal": None,
    "error": None,
    "trace": {"endpoints_contacted": [], "events": [], "metrics": {}},
}
json.dump(envelope, sys.stdout, sort_keys=True, separators=(",", ":"))
sys.stdout.flush()

if MODE in {"escape", "setpgid"}:
    os.close(1)
    os.close(2)
    time.sleep(30)
'''


def write_child(path: Path, *, mode: str, marker: Path) -> Path:
    body = SOURCE.replace("@MODE@", mode).replace("@MARKER@", str(marker))
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path
