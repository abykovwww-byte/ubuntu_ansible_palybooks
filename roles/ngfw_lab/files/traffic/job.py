"""Bound a Docker exec workload, and stop only this runner's process group."""
import argparse
import json
import os
from pathlib import Path
import re
import signal
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["run", "stop"])
    parser.add_argument("job")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if not re.fullmatch(r"[a-f0-9]{32}", args.job):
        parser.error("invalid job id")
    path = Path("/tmp") / ("ngfw-" + args.job + ".json")
    cancelled = path.with_suffix(".cancel")
    if args.action == "stop":
        # Covers cancellation before Docker has actually started the exec process.
        cancelled.touch(exist_ok=True)
        if path.exists():
            try:
                state = json.loads(path.read_text())
                # Verify /proc starttime as protection against recycled PIDs.
                proc = Path(f"/proc/{state['pid']}/stat")
                if proc.exists() and proc.read_text().rsplit(")", 1)[1].split()[19] == state["start"]:
                    os.killpg(state["pid"], signal.SIGKILL)
            except (ProcessLookupError, json.JSONDecodeError):
                pass
        return
    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not 1 <= args.timeout <= 3620 or not command or command[0] not in ("iperf3", "python3"):
        parser.error("invalid bounded job")
    if cancelled.exists():
        cancelled.unlink()
        raise SystemExit(125)
    # Exclusive marker prevents duplicate jobs. The runner also holds a host lock.
    with path.open("x", encoding="utf-8") as marker:
        child = subprocess.Popen(command, start_new_session=True)
        try:
            start = Path(f"/proc/{child.pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
            json.dump({"pid": child.pid, "start": start}, marker)
            marker.flush()
            try:
                code = 125 if cancelled.exists() else child.wait(timeout=args.timeout)
            except subprocess.TimeoutExpired:
                code = 124
            finally:
                if child.poll() is None:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait()
        finally:
            path.unlink(missing_ok=True)
            cancelled.unlink(missing_ok=True)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
