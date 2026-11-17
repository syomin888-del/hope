import faulthandler
import os
import runpy
import sqlite3
import sys

SOURCE = "/app/oracle_fonbet_full.py"
TEMP = "/data/eva-tmp/fonbet-sqlite"

if not (os.path.isdir(TEMP) and os.path.realpath(TEMP) == TEMP and os.access(TEMP, os.W_OK | os.X_OK)):
    raise RuntimeError("EVA_SQLITE_TEMP_DIRECTORY_UNAVAILABLE")
os.environ["SQLITE_TMPDIR"] = TEMP
print("EVA_FONBET_SQLITE_TEMP_R1 dir=" + TEMP, file=sys.stderr, flush=True)

reported = False

def on_exception(frame, event, arg):
    global reported
    if event == "exception" and not reported:
        _, error, _ = arg
        if isinstance(error, sqlite3.OperationalError) and (
            getattr(error, "sqlite_errorcode", None) == sqlite3.SQLITE_FULL or "database or disk is full" in str(error)
        ):
            reported = True
            metrics = {}
            for label, path in (("temp", TEMP), ("data", "/data"), ("legacy_tmp", "/tmp")):
                try:
                    info = os.statvfs(path)
                    metrics[label] = info.f_bavail * info.f_frsize
                except OSError:
                    metrics[label] = None
            print("EVA_SQLITE_FULL location=" + frame.f_code.co_name + ":" + str(frame.f_lineno)
                  + " free_bytes=" + str(metrics), file=sys.stderr, flush=True)
    return on_exception

def on_call(frame, event, arg):
    if event == "call" and frame.f_code.co_filename == SOURCE:
        frame.f_trace_lines = False
        return on_exception
    return None

sys.path.insert(0, os.path.dirname(SOURCE))
sys.settrace(on_call)
try:
    faulthandler.dump_traceback_later(60, repeat=True)
    runpy.run_path(SOURCE, run_name="__main__")
finally:
    faulthandler.cancel_dump_traceback_later()
    sys.settrace(None)
