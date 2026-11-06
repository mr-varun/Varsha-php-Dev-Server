"""Run one MySQL Rescue job for varsha and stream what happens as JSON lines.

Runs under the MySQL rescue tool's own Python (bin\\mysql-rescue\\runtime\\python64), standard
library only, no varsha imports:

    python.exe -I mysql_rescue_runner.py <tool folder>     (one JSON request on the first stdin line)

The request: {"folder", "mode": recover|force|diagnose, "out", "servers": [folders with builds]}.
stdout, one JSON object per line: {"type": "job", "dir"} once the job folder exists, then
{"type": "progress", "stage", "done", "total", "note"} and {"type": "log", "line"}, and finally
{"type": "done", "status", "dir"} (or {"type": "error", "message"}). Writing "stop" to stdin, or
closing it (varsha quit), sets the tool's own stop event: it stops at the next safe point, stops its
temporary server and still writes its report.
"""

import json
import sys
import threading
from pathlib import Path

_out_lock = threading.Lock()


def emit(obj):
    with _out_lock:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()


def main():
    tool = Path(sys.argv[1])
    request = json.loads(sys.stdin.readline())
    sys.path.insert(0, str(tool / "app"))
    import mysql_rescue  # the tool's own entry point (its folder is on sys.path now)

    stop = threading.Event()

    def watch_stdin():
        for line in sys.stdin:
            if line.strip() == "stop":
                stop.set()
        stop.set()  # stdin closed: varsha is gone, so stop safely

    threading.Thread(target=watch_stdin, daemon=True).start()

    def hook(job):
        job.cancel_event = stop
        job.listeners.append(lambda line: emit({"type": "log", "line": line}))
        job.progress_listeners.append(
            lambda stage, done, total, note="": emit({"type": "progress", "stage": stage, "done": done,
                                                      "total": total, "note": str(note or "")}))
        emit({"type": "job", "dir": str(job.dir)})

    try:
        job, report = mysql_rescue.run_pipeline(
            request["folder"], mode=request.get("mode") or "recover", out=request["out"],
            extra_server_dirs=request.get("servers") or [], job_hook=hook, echo=False)
        emit({"type": "done", "status": report.get("status"), "dir": str(job.dir)})
    except Exception as exc:  # e.g. not a data folder; the tool reports everything else itself
        emit({"type": "error", "message": str(exc)})
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
