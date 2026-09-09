#!/usr/bin/env python3
"""Dev tool: execute Python in a running Isaac Sim via the NVIDIA vscode bridge (port 8226).

Isaac 5.1 framing: server executes on first TCP chunk -> keep payloads small.
For big code, write a file and send: exec(open(path).read())

Usage:
  kit_exec.py 'print("hi")'          # code as argument
  kit_exec.py -f script.py           # send whole file (risky if > one chunk; prefer --indirect)
  kit_exec.py -F script.py           # indirect: exec(open('script.py').read()) server-side
  echo 'code' | kit_exec.py          # code from stdin
"""

import json
import socket
import sys


def kit_exec(code: str, host="127.0.0.1", port=8226, timeout=120.0) -> dict:
    with socket.create_connection((host, port), timeout=timeout) as s:
        s.sendall(code.encode())
        # no half-close: 5.1 executes on first chunk and eof_received would kill
        # the transport before the (async) reply is written
        chunks = []
        while True:
            b = s.recv(65536)
            if not b:
                break
            chunks.append(b)
    return json.loads(b"".join(chunks).decode())


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "-f":
        code = open(args[1]).read()
    elif args and args[0] == "-F":
        import os

        code = f"exec(open({os.path.abspath(args[1])!r}).read())"
    elif args:
        code = " ".join(args)
    else:
        code = sys.stdin.read()
    r = kit_exec(code)
    status = r.get("status")
    out = r.get("output", "")
    if out:
        print(out, end="" if out.endswith("\n") else "\n")
    if "result" in r and r["result"] is not None:
        print("result:", r["result"])
    if status != "ok":
        for line in r.get("traceback", []):
            print(line, end="", file=sys.stderr)
        print(f"{r.get('ename')}: {r.get('evalue')}", file=sys.stderr)
        sys.exit(1)
