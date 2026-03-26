"""
Run this script to stop the quiz server before restarting it.
Usage:  python kill_server.py
"""
import os
import signal
import subprocess
import time

PORT = 8000

result = subprocess.run(["lsof", "-ti", f":{PORT}"], capture_output=True, text=True)
pids = [p for p in result.stdout.strip().splitlines() if p]

if not pids:
    print(f"No process found on port {PORT}.")
else:
    for pid_str in pids:
        pid = int(pid_str)
        os.kill(pid, signal.SIGTERM)
        print(f"Killed process on port {PORT} (PID {pid})")
    time.sleep(1)   # wait for port to be released
    print("Done. You can now start the server.")
