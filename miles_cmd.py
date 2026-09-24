"""Send a typed command to the running Miles, e.g.:  python miles_cmd.py "open spotify"

Handy for Stream Deck buttons, AutoHotkey, or scheduled tasks.
"""
import socket
import sys

import config

text = " ".join(sys.argv[1:]).strip()
if not text:
    sys.exit('Usage: python miles_cmd.py "your command"')
try:
    with socket.create_connection(("127.0.0.1", config.COMMAND_PORT), timeout=3) as s:
        s.sendall(text.encode("utf-8"))
        print(s.recv(16).decode().strip())
except OSError:
    sys.exit("Miles isn't running.")
