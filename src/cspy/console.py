"""Send Python to the console running inside CSP.

    python console.py "code"          run a snippet (expressions echo their value)
    python console.py file.py         run a script file
    python console.py name            run scripts/name.py
    python console.py                 interactive prompt
"""
import os
import socket
import sys

PORT = 50555
sys.stdout.reconfigure(encoding="utf-8")  # CSP menu titles hold non-ANSI characters (e.g. U+25CB)
SCRIPTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts")


def send(code):
    with socket.create_connection(("127.0.0.1", PORT), timeout=60) as s:
        s.sendall(code.encode() + b"\0")
        data = b""
        while not data.endswith(b"\0"):
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
    return data.rstrip(b"\0").decode()


def script_path(arg):
    for p in (arg, os.path.join(SCRIPTS, arg), os.path.join(SCRIPTS, arg + ".py")):
        if p.endswith(".py") and os.path.isfile(p):
            return os.path.abspath(p)


if len(sys.argv) > 1:
    arg = " ".join(sys.argv[1:])
    path = script_path(arg)
    if path:
        with open(path, encoding="utf-8") as f:
            arg = f"__file__ = {path!r}\n" + f.read()
    print(send(arg), end="")
else:
    while True:
        try:
            line = input("csp> ")
        except EOFError:
            break
        print(send(line), end="")
