#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Local MCP gateway + async job runner for a local Stata MCP server.

Why this exists
---------------
The upstream Stata MCP server executes a ``.do`` file as one blocking MCP tool
call with a fixed default timeout. Any long run therefore either hits the
server timeout or the *client* timeout (some hosts cancel MCP tool calls after
30 seconds) and, in both cases, returns nothing until it is over.

This process sits between the MCP client and the upstream engine:

    MCP client ──> this gateway (client-facing port) ──> upstream engine (port+1)

It keeps the upstream tool names working, but runs every ``.do`` file as a
background job and records each Stata step (the ``. command`` lines that Stata
writes into its log) to disk while the job runs. Callers can poll
``stata_job_tail`` and get exactly the steps that happened since their last
poll, no matter how long the job takes.

Files
-----
``<home>/jobs/<job_id>/records.jsonl``  one JSON object per log line
``<home>/jobs/<job_id>/journal.log``    human-readable step journal (tail -f)
``<home>/jobs/<job_id>/meta.json``      job metadata and status

Configuration
-------------
CLI:  ``--home DIR --host H --port N --engine-port N --log-file FILE``
State: ``<home>/state.json`` (written by ``install.py``) provides engine port,
       default timeout and sync wait when the flags are omitted.

The gateway never talks to anything except the loopback engine port, and never
reads or writes another application's configuration files.
"""

from __future__ import annotations

import argparse
import http.client
import json
import logging
import os
import re
import shutil
import sys
import threading
import time
import traceback
import uuid
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

_BIN_DIR = os.path.dirname(os.path.abspath(__file__))
if _BIN_DIR not in sys.path:
    sys.path.insert(0, _BIN_DIR)

import stata_mcp_analysis  # noqa: E402

GATEWAY_VERSION = "1.2.3"
TOOLS_COUNT = 17
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4001
DEFAULT_ENGINE_PORT = 4002
DEFAULT_TIMEOUT = 21600.0  # 6 hours
DEFAULT_SYNC_WAIT = 20.0  # must stay below typical client tool timeouts
MAX_SYNC_WAIT = 25.0
MAX_TAIL_LIMIT = 500
MAX_RESULT_CHARS = 24000
RECORD_TEXT_LIMIT = 4000
JOB_KEEP_DAYS = 7
JOB_KEEP_COUNT = 200
HEALTH_PATH = "/health"
MCP_PATH = "/mcp-streamable"


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------
def resolve_home(explicit=None):
    for value in (explicit, os.environ.get("STATA_MCP_HOME")):
        if value:
            return os.path.abspath(os.path.expanduser(str(value)))
    return os.path.abspath(os.path.expanduser(os.path.join("~", ".stata-mcp")))


def load_state(home):
    path = os.path.join(home, "state.json")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _as_float(value, fallback):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return number if number > 0 else fallback


def _as_nonneg_float(value, fallback):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return number if number >= 0 else fallback


class Config(object):
    def __init__(self, args):
        self.home = resolve_home(args.home)
        state = load_state(self.home)

        self.host = args.host or os.environ.get("STATA_MCP_HOST") or state.get("host") or DEFAULT_HOST
        self.port = int(args.port or os.environ.get("STATA_MCP_PORT") or state.get("port") or DEFAULT_PORT)
        engine_port = args.engine_port or state.get("engine_port") or (self.port + 1)
        try:
            self.engine_port = int(engine_port)
        except (TypeError, ValueError):
            self.engine_port = DEFAULT_ENGINE_PORT

        self.default_timeout = _as_float(
            args.default_timeout or state.get("default_timeout"), DEFAULT_TIMEOUT
        )
        self.sync_wait = min(
            MAX_SYNC_WAIT,
            _as_nonneg_float(args.sync_wait if args.sync_wait is not None else state.get("sync_wait"), DEFAULT_SYNC_WAIT),
        )
        self.jobs_dir = os.path.join(self.home, "jobs")
        self.selection_dir = os.path.join(self.home, "cache", "selection")
        self.engine_log_file = state.get("log_file") or os.path.join(self.home, "logs", "server.log")
        self.stata_path = state.get("stata_path") or ""
        self.version = GATEWAY_VERSION

    def describe(self):
        return "gateway=:%d engine=:%d home=%s" % (self.port, self.engine_port, self.home)


# ---------------------------------------------------------------------------
# engine client (loopback only, never proxied)
# ---------------------------------------------------------------------------
class EngineError(RuntimeError):
    pass


def _engine_hint(cfg):
    return "run `python3 %s` to restart the runtime" % os.path.join(cfg.home, "bin", "stata-mcp-start.py")


def _engine_connection(cfg, timeout):
    return http.client.HTTPConnection(cfg.host, cfg.engine_port, timeout=timeout)


def engine_health(cfg, timeout=3.0):
    try:
        connection = _engine_connection(cfg, timeout)
        try:
            connection.request("GET", HEALTH_PATH, headers={"Accept": "application/json"})
            response = connection.getresponse()
            body = response.read()
            if response.status != 200:
                return None
            return json.loads(body.decode("utf-8", "replace"))
        finally:
            connection.close()
    except (OSError, http.client.HTTPException, ValueError):
        return None


def engine_post_json(cfg, path, payload, timeout=60.0):
    body = json.dumps(payload).encode("utf-8")
    connection = _engine_connection(cfg, timeout)
    try:
        connection.request(
            "POST",
            path,
            body=body,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        response = connection.getresponse()
        raw = response.read().decode("utf-8", "replace")
        if response.status >= 400:
            raise EngineError("engine HTTP %d: %s" % (response.status, raw[:400]))
        try:
            return json.loads(raw)
        except ValueError:
            return {"status": "success", "result": raw}
    except (OSError, http.client.HTTPException) as exc:
        raise EngineError(
            "cannot reach the Stata engine on %s:%d (%s); %s"
            % (cfg.host, cfg.engine_port, exc, _engine_hint(cfg))
        )
    finally:
        try:
            connection.close()
        except Exception:
            pass


def engine_stop(cfg, session_id, timeout=15.0):
    query = urlencode({"session_id": session_id}) if session_id else ""
    path = "/stop_execution" + (("?" + query) if query else "")
    connection = _engine_connection(cfg, timeout)
    try:
        connection.request("POST", path, body=b"", headers={"Accept": "application/json"})
        response = connection.getresponse()
        raw = response.read().decode("utf-8", "replace")
        try:
            return json.loads(raw)
        except ValueError:
            return {"status": "unknown", "raw": raw[:400]}
    except (OSError, http.client.HTTPException) as exc:
        raise EngineError("cannot reach the Stata engine (%s); %s" % (exc, _engine_hint(cfg)))
    finally:
        try:
            connection.close()
        except Exception:
            pass


def engine_get_text(cfg, path, timeout=30.0):
    connection = _engine_connection(cfg, timeout)
    try:
        connection.request("GET", path, headers={"Accept": "text/plain, application/json"})
        response = connection.getresponse()
        raw = response.read().decode("utf-8", "replace")
        return response.status, raw
    except (OSError, http.client.HTTPException) as exc:
        raise EngineError("cannot reach the Stata engine (%s); %s" % (exc, _engine_hint(cfg)))
    finally:
        try:
            connection.close()
        except Exception:
            pass


def engine_get_json(cfg, path, timeout=30.0):
    status, raw = engine_get_text(cfg, path, timeout=timeout)
    if status >= 400:
        raise EngineError("engine HTTP %d: %s" % (status, raw[:400]))
    try:
        return json.loads(raw)
    except ValueError:
        return {"status": "error", "message": raw[:400]}


def _unescape_stream_line(text):
    """The engine doubles backslashes so each SSE line stays a single line."""
    return text.replace("\\\\", "\\")


def _strip_log_banner(lines):
    """Drop the Stata log header/footer the engine adds around user output."""
    start = 0
    head = [line for line in lines[:12]]
    if head and head[0].startswith("---"):
        for index, line in enumerate(lines[:12]):
            if "opened on:" in line:
                start = index + 1
                break
    end = len(lines)
    for index in range(len(lines) - 1, max(start, len(lines) - 6) - 1, -1):
        stripped = lines[index].strip()
        if stripped in (".", "") or stripped.startswith(". capture log close"):
            end = index
        else:
            break
    return [line for line in lines[start:end] if line.strip() not in (".",)]


# ---------------------------------------------------------------------------
# job model
# ---------------------------------------------------------------------------
RUNNING = "running"
FINISHED = "finished"
FAILED = "failed"
TIMED_OUT = "timed_out"
CANCELLED = "cancelled"
INTERRUPTED = "interrupted"

_ACTIVE_STATUSES = (RUNNING,)


class Job(object):
    def __init__(
        self,
        cfg,
        kind,
        file_path,
        working_dir="",
        session_id="",
        timeout=None,
        source_file="",
        line_offset=0,
    ):
        self.cfg = cfg
        self.kind = kind
        self.file_path = os.path.abspath(os.path.expanduser(file_path))
        self.source_file = os.path.abspath(os.path.expanduser(source_file)) if source_file else self.file_path
        self.line_offset = int(line_offset or 0)
        self.working_dir = working_dir or ""
        self.session_id = session_id or ""
        self.timeout = _as_float(timeout, cfg.default_timeout)
        self.started_at = time.time()
        self.job_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        self.directory = os.path.join(cfg.jobs_dir, self.job_id)
        self.records_path = os.path.join(self.directory, "records.jsonl")
        self.journal_path = os.path.join(self.directory, "journal.log")
        self.meta_path = os.path.join(self.directory, "meta.json")
        self.error_path = os.path.join(self.directory, "error.json")

        self.status = RUNNING
        self.note = ""
        self.seq = 0
        self.step = 0
        self.current = ""
        self.error_code = ""
        self.error_line = None
        self.error_message = ""
        self.error_diagnosis = None
        self._ended_at = None
        self._cancel_requested = False
        self._header_open = False
        self._lock = threading.Lock()
        self._thread = None

        os.makedirs(self.directory, exist_ok=True)
        try:
            os.chmod(self.directory, 0o700)
        except OSError:
            pass
        self._write_meta()

    # -- persistence ------------------------------------------------------
    def elapsed(self):
        return time.time() - self.started_at

    def meta(self):
        return {
            "job_id": self.job_id,
            "kind": self.kind,
            "file_path": self.file_path,
            "source_file": self.source_file,
            "line_offset": self.line_offset,
            "working_dir": self.working_dir,
            "session_id": self.session_id,
            "timeout": self.timeout,
            "started_at": self.started_at,
            "started_at_text": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.started_at)),
            "ended_at": (self.started_at + self._ended_at) if self._ended_at else None,
            "status": self.status,
            "step": self.step,
            "records": self.seq,
            "current": self.current,
            "note": self.note,
            "error_code": self.error_code,
            "error_line": self.error_line,
            "error_message": self.error_message,
            "journal": self.journal_path,
            "records_path": self.records_path,
        }

    def _write_meta(self):
        payload = self.meta()
        tmp = self.meta_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=1)
            os.replace(tmp, self.meta_path)
        except OSError:
            pass

    def _append_record(self, kind, text):
        text = (text or "").rstrip("\r\n")
        if not text:
            return
        if len(text) > RECORD_TEXT_LIMIT:
            text = text[:RECORD_TEXT_LIMIT] + " …"

        with self._lock:
            self.seq += 1
            if kind == "command":
                self.step += 1
                self.current = text
            record = {
                "seq": self.seq,
                "step": self.step,
                "t": round(self.elapsed(), 2),
                "kind": kind,
                "text": text,
            }
            line = json.dumps(record, ensure_ascii=False) + "\n"
            try:
                with open(self.records_path, "a", encoding="utf-8") as handle:
                    handle.write(line)
            except OSError:
                pass
            try:
                with open(self.journal_path, "a", encoding="utf-8") as handle:
                    stamp = "[%02d:%05.2f]" % (int(record["t"]) // 60, record["t"] % 60)
                    if kind == "command":
                        handle.write("%s STEP %-4d %s\n" % (stamp, self.step, text))
                    elif kind in ("error", "marker", "graph"):
                        handle.write("%s %-8s %s\n" % (stamp, kind.upper(), text))
                    else:
                        handle.write("%s          %s\n" % (stamp, text))
            except OSError:
                pass

            should_flush_meta = kind == "command" or self.seq % 20 == 0
            if should_flush_meta:
                self._write_meta()

    def _compute_error_diagnosis(self):
        records = self.read_records()
        src_text = ""
        target = self.source_file or self.file_path
        if target and os.path.isfile(target):
            try:
                with open(target, "r", encoding="utf-8", errors="replace") as handle:
                    src_text = handle.read()
            except OSError:
                src_text = ""
        diag = stata_mcp_analysis.locate_error_in_do(
            src_text,
            records,
            file_path=target,
            line_offset=self.line_offset,
        )
        if not diag:
            return None
        try:
            tmp = self.error_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(diag, handle, ensure_ascii=False, indent=1)
            os.replace(tmp, self.error_path)
        except OSError:
            pass
        return diag

    def _finish(self, status, note=""):
        diag = None
        if status == FAILED:
            diag = self._compute_error_diagnosis()
        with self._lock:
            if self.status != RUNNING:
                return
            if self._cancel_requested and status in (FINISHED, FAILED, INTERRUPTED, TIMED_OUT):
                note = note or "stop requested by the client"
                status = CANCELLED
            self.status = status
            self.note = note
            if diag:
                self.error_diagnosis = diag
                self.error_code = diag.get("error_code", "")
                self.error_line = diag.get("line")
                self.error_message = diag.get("error_message", "")
            self._ended_at = self.elapsed()
        self._write_meta()

    # -- execution --------------------------------------------------------
    def start(self):
        self._thread = threading.Thread(target=self._run, name="job-" + self.job_id, daemon=True)
        self._thread.start()
        return self

    def _run(self):
        try:
            self._execute_stream()
        except EngineError as exc:
            self._append_record("error", "ERROR: %s" % exc)
            self._finish(FAILED, str(exc))
        except Exception as exc:  # noqa: BLE001 - keep the job alive for diagnostics
            self._append_record("error", "ERROR: unexpected gateway failure: %s" % exc)
            logging.error("job %s failed: %s\n%s", self.job_id, exc, traceback.format_exc())
            self._finish(FAILED, str(exc))

    def _stream_path(self):
        params = {"file_path": self.file_path, "timeout": int(self.timeout)}
        if self.working_dir:
            params["working_dir"] = self.working_dir
        if self.session_id:
            params["session_id"] = self.session_id
        return "/run_file/stream?" + urlencode(params)

    def _execute_stream(self):
        # Read timeout must outlast the job itself: Stata can compute for a long
        # time without producing new log lines.
        read_timeout = max(120.0, self.timeout + 300.0)
        connection = _engine_connection(self.cfg, read_timeout)
        try:
            connection.request("GET", self._stream_path(), headers={"Accept": "text/event-stream"})
            response = connection.getresponse()
            content_type = (response.getheader("content-type") or "").lower()
            if response.status >= 400:
                body = response.read().decode("utf-8", "replace")
                raise EngineError("engine HTTP %d while starting the job: %s" % (response.status, body[:400]))
            if "text/event-stream" not in content_type:
                body = response.read().decode("utf-8", "replace")
                for line in body.splitlines():
                    self._consume(line)
                self._finish(FINISHED if self.status == RUNNING else self.status)
                return

            saw_completion = False
            saw_error = False
            while True:
                raw = response.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if not line:
                    continue
                if line.startswith("data:"):
                    payload = line[5:]
                    if payload.startswith(" "):
                        payload = payload[1:]
                    payload = _unescape_stream_line(payload)
                    if not payload.strip():
                        continue
                    kind = self._consume(payload)
                    if kind == "done":
                        saw_completion = True
                    elif kind == "error":
                        saw_error = True
                elif line.startswith(("event:", "id:", "retry:", ":")):
                    continue
                else:
                    self._consume(_unescape_stream_line(line))

            if self.status != RUNNING:
                return
            fatal_err = stata_mcp_analysis.detect_stata_error(self.read_records())
            if saw_error or fatal_err:
                note = "the engine reported an error"
                if fatal_err:
                    note = "Stata %s: %s" % (fatal_err.get("error_code", ""), fatal_err.get("error_message", ""))
                self._finish(FAILED, note)
            elif saw_completion:
                self._finish(FINISHED)
            else:
                self._finish(INTERRUPTED, "the engine stream closed before completion")
        finally:
            try:
                connection.close()
            except Exception:
                pass

    def _consume(self, text):
        """Classify one streamed line. Returns "done" / "error" / None."""
        stripped = text.strip()
        if stripped.startswith("Starting execution of"):
            self._header_open = True
            self._append_record("marker", stripped)
            self._write_meta()
            return None
        if self._header_open:
            # Stata's log banner (name/log/type/opened on) is engine noise, not a step.
            if "opened on:" in stripped:
                self._header_open = False
                return None
            if not stripped.startswith((". ", "> ")):
                return None
            self._header_open = False
        if stripped in (".", ">", ""):
            return None
        if stripped.startswith("*** Execution completed ***"):
            self._append_record("marker", stripped)
            return "done"
        if stripped.startswith("ERROR:"):
            self._append_record("error", stripped)
            if "timed out" in stripped.lower():
                self._finish(TIMED_OUT, stripped)
            else:
                self._finish(FAILED, stripped)
            return "error"
        if stripped.startswith("GRAPHS DETECTED:") or " graph(s) created" in stripped:
            self._append_record("graph", stripped)
            return None
        if re.match(r"^r\(\d+\);", stripped):
            self._append_record("error", stripped)
            return None
        if stripped.startswith(("•", "=" * 10)) or stripped.lower().startswith("graph "):
            self._append_record("graph", stripped)
            return None
        if stripped.startswith((". ", "> ")):
            command = stripped[2:].strip()
            if re.match(r"(?i)^(capture\s+)?log\s+close\b", command):
                # The engine closes its own log around every run; not a user step.
                return None
            self._append_record("command", stripped)
            return None
        self._append_record("output", text)
        return None

    # -- reads ------------------------------------------------------------
    def read_records(self):
        try:
            with open(self.records_path, "r", encoding="utf-8", errors="replace") as handle:
                lines = handle.readlines()
        except OSError:
            return []
        records = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except ValueError:
                continue
        return records

    def result_text(self):
        records = self.read_records()
        lines = [record.get("text", "") for record in records if record.get("kind") in ("command", "output", "error", "graph")]
        lines = _strip_log_banner(lines)
        body = "\n".join(lines)
        if len(body) > MAX_RESULT_CHARS:
            body = "… (earlier output truncated)\n" + body[-MAX_RESULT_CHARS:]
        if self.error_diagnosis:
            diag_text = stata_mcp_analysis.format_error_diagnosis(self.error_diagnosis)
            if diag_text:
                body = (body + "\n\n" + diag_text).strip()
        return body

    def summary(self, wait_seconds=0.0):
        if wait_seconds and self.status == RUNNING:
            deadline = time.time() + min(max(wait_seconds, 0.0), MAX_SYNC_WAIT)
            while time.time() < deadline and self.status == RUNNING:
                time.sleep(0.2)
        with self._lock:
            payload = {
                "job_id": self.job_id,
                "status": self.status,
                "kind": self.kind,
                "file_path": self.file_path,
                "source_file": self.source_file,
                "line_offset": self.line_offset,
                "step": self.step,
                "records": self.seq,
                "elapsed": round(self.elapsed(), 1),
                "timeout": self.timeout,
                "current": self.current,
                "note": self.note,
                "journal": self.journal_path,
            }
            if self.error_code:
                payload["error_code"] = self.error_code
            if self.error_line is not None:
                payload["error_line"] = self.error_line
            if self.error_message:
                payload["error_message"] = self.error_message
            return payload

    def wait(self, seconds):
        if seconds <= 0:
            return
        deadline = time.time() + min(max(seconds, 0.0), MAX_SYNC_WAIT)
        while time.time() < deadline and self.status == RUNNING:
            time.sleep(0.2)


class ReadOnlyJob(object):
    """A finished/interrupted job loaded from disk (e.g. after a gateway restart)."""

    def __init__(self, cfg, directory):
        self.cfg = cfg
        self.directory = directory
        self.records_path = os.path.join(directory, "records.jsonl")
        self.journal_path = os.path.join(directory, "journal.log")
        self.error_path = os.path.join(directory, "error.json")
        meta_path = os.path.join(directory, "meta.json")
        self._lock = threading.Lock()
        try:
            with open(meta_path, "r", encoding="utf-8") as handle:
                self._meta = json.load(handle)
        except (OSError, ValueError):
            self._meta = {}
        self.job_id = self._meta.get("job_id") or os.path.basename(directory)
        self.status = self._meta.get("status") or FINISHED
        self.kind = self._meta.get("kind") or "file"
        self.file_path = self._meta.get("file_path") or ""
        self.source_file = self._meta.get("source_file") or self.file_path
        self.line_offset = int(self._meta.get("line_offset") or 0)
        self.step = int(self._meta.get("step") or 0)
        self.seq = int(self._meta.get("records") or 0)
        self.current = self._meta.get("current") or ""
        self.note = self._meta.get("note") or ""
        self.error_code = self._meta.get("error_code") or ""
        self.error_line = self._meta.get("error_line")
        self.error_message = self._meta.get("error_message") or ""
        self.started_at = float(self._meta.get("started_at") or time.time())
        self.timeout = float(self._meta.get("timeout") or cfg.default_timeout)
        self.error_diagnosis = None
        if os.path.isfile(self.error_path):
            try:
                with open(self.error_path, "r", encoding="utf-8") as handle:
                    self.error_diagnosis = json.load(handle)
            except (OSError, ValueError):
                self.error_diagnosis = None

    def elapsed(self):
        return max(0.0, time.time() - self.started_at)

    def read_records(self):
        try:
            with open(self.records_path, "r", encoding="utf-8", errors="replace") as handle:
                lines = handle.readlines()
        except OSError:
            return []
        records = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except ValueError:
                continue
        return records

    def summary(self, wait_seconds=0.0):
        payload = {
            "job_id": self.job_id,
            "status": self.status,
            "kind": self.kind,
            "file_path": self.file_path,
            "source_file": self.source_file,
            "line_offset": self.line_offset,
            "step": self.step,
            "records": len(self.read_records()),
            "elapsed": round(self.elapsed(), 1),
            "timeout": self.timeout,
            "current": self.current,
            "note": self.note,
            "journal": self.journal_path,
        }
        if self.error_code:
            payload["error_code"] = self.error_code
        if self.error_line is not None:
            payload["error_line"] = self.error_line
        if self.error_message:
            payload["error_message"] = self.error_message
        return payload


class JobRegistry(object):
    def __init__(self, cfg):
        self.cfg = cfg
        self._lock = threading.Lock()
        self._jobs = {}
        os.makedirs(cfg.jobs_dir, exist_ok=True)
        os.makedirs(cfg.selection_dir, exist_ok=True)
        self._mark_orphans()
        self._cleanup()
        self._cleanup_selections()

    # -- lifecycle --------------------------------------------------------
    @staticmethod
    def _is_job_dir(name):
        return bool(name) and not name.startswith(("_", "."))

    def _cleanup_selections(self):
        """Staged selections are scratch input; drop old ones."""
        deadline = time.time() - 2 * 86400
        try:
            entries = os.listdir(self.cfg.selection_dir)
        except OSError:
            return
        for name in entries:
            path = os.path.join(self.cfg.selection_dir, name)
            try:
                if os.path.getmtime(path) < deadline:
                    os.unlink(path)
            except OSError:
                continue

    def _mark_orphans(self):
        """Jobs still marked RUNNING belong to a previous gateway process."""
        try:
            entries = sorted(os.listdir(self.cfg.jobs_dir))
        except OSError:
            return
        for name in entries:
            if not self._is_job_dir(name):
                continue
            directory = os.path.join(self.cfg.jobs_dir, name)
            meta_path = os.path.join(directory, "meta.json")
            try:
                with open(meta_path, "r", encoding="utf-8") as handle:
                    meta = json.load(handle)
            except (OSError, ValueError):
                continue
            if meta.get("status") == RUNNING:
                meta["status"] = INTERRUPTED
                meta["note"] = "the gateway restarted while this job was running"
                try:
                    with open(meta_path, "w", encoding="utf-8") as handle:
                        json.dump(meta, handle, ensure_ascii=False, indent=1)
                except OSError:
                    pass

    def _cleanup(self):
        keep = []
        try:
            entries = sorted(os.listdir(self.cfg.jobs_dir))
        except OSError:
            return
        for name in entries:
            if not self._is_job_dir(name):
                continue
            directory = os.path.join(self.cfg.jobs_dir, name)
            if not os.path.isdir(directory):
                continue
            try:
                mtime = os.path.getmtime(directory)
            except OSError:
                continue
            keep.append((mtime, directory))
        keep.sort(reverse=True)
        deadline = time.time() - JOB_KEEP_DAYS * 86400
        for index, (mtime, directory) in enumerate(keep):
            if index < JOB_KEEP_COUNT and mtime >= deadline:
                continue
            shutil.rmtree(directory, ignore_errors=True)

    def start(
        self,
        kind,
        file_path,
        working_dir="",
        session_id="",
        timeout=None,
        source_file="",
        line_offset=0,
    ):
        job = Job(
            self.cfg,
            kind,
            file_path,
            working_dir,
            session_id,
            timeout,
            source_file=source_file,
            line_offset=line_offset,
        )
        with self._lock:
            self._jobs[job.job_id] = job
        job.start()
        return job

    def get(self, job_id):
        job_id = (job_id or "").strip()
        if not re.match(r"^[A-Za-z0-9._-]+$", job_id):
            return None
        with self._lock:
            job = self._jobs.get(job_id)
        if job is not None:
            return job
        directory = os.path.join(self.cfg.jobs_dir, job_id)
        if os.path.isdir(directory):
            return ReadOnlyJob(self.cfg, directory)
        return None

    def list(self, limit=50):
        rows = []
        with self._lock:
            for job in self._jobs.values():
                rows.append(job.summary())
        known = set(row["job_id"] for row in rows)
        try:
            entries = sorted(os.listdir(self.cfg.jobs_dir), reverse=True)
        except OSError:
            entries = []
        for name in entries:
            if name in known or not self._is_job_dir(name):
                continue
            job = self.get(name)
            if job is not None:
                rows.append(job.summary())
            if len(rows) >= limit:
                break
        rows.sort(key=lambda row: row["job_id"], reverse=True)
        return rows[:limit]

    def counts(self):
        with self._lock:
            running = sum(1 for job in self._jobs.values() if job.status == RUNNING)
        return {"active": len(self._jobs), "running": running}


# ---------------------------------------------------------------------------
# tail rendering
# ---------------------------------------------------------------------------
def _tail_payload(job, offset, limit, mode):
    records = job.read_records()
    total = len(records)
    try:
        offset = int(offset)
    except (TypeError, ValueError):
        offset = 0
    offset = max(0, min(offset, total))
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 200
    limit = max(1, min(limit, MAX_TAIL_LIMIT))

    payload = job.summary() if hasattr(job, "summary") else {}
    payload.update({"offset": offset, "total_records": total})

    if mode == "full":
        window = records[offset:offset + limit]
        payload["records"] = window
        payload["next_offset"] = offset + len(window)
        payload["mode"] = "full"
    else:
        steps = []
        index = offset
        consumed = offset
        while index < total and len(steps) < limit:
            record = records[index]
            kind = record.get("kind")
            if kind in ("command", "error", "graph", "marker"):
                item = {
                    "seq": record.get("seq"),
                    "step": record.get("step"),
                    "t": record.get("t"),
                }
                if kind == "command":
                    item["command"] = record.get("text", "")
                    preview = []
                    look = index + 1
                    while look < total and len(preview) < 4:
                        following = records[look]
                        if following.get("kind") != "output":
                            break
                        preview.append(following.get("text", ""))
                        look += 1
                    item["output_preview"] = preview
                    index = look
                else:
                    item["kind"] = kind
                    item["text"] = record.get("text", "")
                    index += 1
                steps.append(item)
                consumed = index
            else:
                index += 1
                consumed = index
        payload["records"] = steps
        payload["next_offset"] = consumed
        payload["mode"] = "steps"

    if payload["next_offset"] < total:
        payload["next"] = (
            'call stata_job_tail(job_id="%s", offset=%d) to read the next batch'
            % (payload.get("job_id", ""), payload["next_offset"])
        )
    else:
        payload["next"] = 'no newer records; call stata_job_status(job_id="%s") if the job is still running' % (
            payload.get("job_id", "")
        )
    return payload


def _dumps(payload):
    return json.dumps(payload, ensure_ascii=False, indent=1)


def _wait_hint(job):
    return {
        "job_id": job.job_id,
        "status": job.status,
        "step": job.step,
        "records": job.seq,
        "elapsed": round(job.elapsed(), 1),
        "journal": job.journal_path,
        "next": (
            'still running: call stata_job_tail(job_id="%s", offset=0) to read the steps '
            'recorded so far, or stata_job_status(job_id="%s", wait_seconds=20) to wait'
            % (job.job_id, job.job_id)
        ),
    }


def _run_quick_selection(jobs, cfg, code, working_dir="", session_id="", timeout=30.0):
    """Run a short diagnostic Stata snippet through the job runner and wait."""
    staging = cfg.selection_dir
    os.makedirs(staging, exist_ok=True)
    path = os.path.join(staging, "diag-%s.do" % uuid.uuid4().hex[:8])
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(code if code.endswith("\n") else code + "\n")
    job = jobs.start("selection", path, working_dir, session_id, timeout=timeout)
    deadline = time.time() + min(max(float(timeout), 2.0), MAX_SYNC_WAIT)
    while time.time() < deadline and job.status == RUNNING:
        time.sleep(0.15)
    return job


def _format_table_preview(columns, rows, max_rows=20):
    if not columns:
        return "(no columns)"
    shown = rows[:max_rows]
    cols = list(columns)
    header = "| " + " | ".join(str(c) for c in cols) + " |"
    sep = "| " + " | ".join("---" for _ in cols) + " |"
    body = []
    for row in shown:
        if isinstance(row, dict):
            vals = [str(row.get(c, "")) for c in cols]
        elif isinstance(row, (list, tuple)):
            vals = [str(row[i]) if i < len(row) else "" for i in range(len(cols))]
        else:
            vals = [str(row)]
        vals = [v.replace("\n", " ").replace("|", "\\|")[:60] for v in vals]
        body.append("| " + " | ".join(vals) + " |")
    return "\n".join([header, sep] + body)


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------
def build_server(cfg):
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP(
        name="stata-mcp",
        instructions=(
            "Local Stata MCP gateway (17 tools). Best practices:\n"
            "1. Before writing unfamiliar commands or options, call `stata_help(topic, section)` or "
            "`stata_search_help(query)` / `stata_which(command)` to verify exact Stata syntax.\n"
            "2. NEVER write observation-level `1/_N` or `levelsof`+`foreach` loops! Prefer `bysort`, "
            "`egen`, `collapse`, `i.var`, `L./D.`, `frames`, or `mata`. Call `stata_lint_do` before "
            "running complex .do files and `stata_vectorize_guide` for idiomatic replacements.\n"
            "3. Long .do files run asynchronously: `stata_run_file` / `stata_run_lines` / `stata_run_selection` "
            "return a job_id after 20s; poll with `stata_job_tail(job_id, offset)`.\n"
            "4. On any error, read `[ERROR LOCATION]` or call `stata_diagnose_error` to jump to the "
            "exact failing line, fix it, and re-test quickly with `stata_run_lines`."
        ),
        host=cfg.host,
        port=cfg.port,
        streamable_http_path=MCP_PATH,
        log_level="WARNING",
    )
    jobs = JobRegistry(cfg)

    # -- health -----------------------------------------------------------
    @mcp.custom_route(HEALTH_PATH, methods=["GET"])
    async def health_route(request):  # noqa: ARG001 - FastMCP passes the request
        from starlette.responses import JSONResponse

        engine = engine_health(cfg, timeout=2.0)
        return JSONResponse(
            {
                "status": "ok" if engine else "degraded",
                "version": cfg.version,
                "tools_count": TOOLS_COUNT,
                "stata_path": cfg.stata_path,
                "gateway": {"port": cfg.port, "pid": os.getpid()},
                "engine": {
                    "port": cfg.engine_port,
                    "healthy": bool(engine),
                    "detail": engine or None,
                },
                "jobs": jobs.counts(),
                "default_timeout": cfg.default_timeout,
                "sync_wait": cfg.sync_wait,
            }
        )

    # -- tools ------------------------------------------------------------
    @mcp.tool(
        name="stata_run_file",
        description=(
            "Run a Stata .do file. Short files return output directly; long files run as a "
            "background job returning job_id (follow with stata_job_tail). Failures include exact "
            "[ERROR LOCATION] line numbers. Pass lint=True to pre-check anti-loop/syntax issues."
        ),
    )
    def stata_run_file(
        file_path: str,
        timeout: float = 0,
        working_dir: str = "",
        session_id: str = "",
        wait_seconds: float = -1,
        lint: bool = False,
    ) -> str:
        target = os.path.abspath(os.path.expanduser(file_path or ""))
        if not target or not os.path.isfile(target):
            return "Error: file not found: %s" % (file_path or "<empty>")
        if not target.lower().endswith(".do"):
            return "Error: %s is not a .do file" % target
        lint_banner = ""
        if lint:
            try:
                src_text = open(target, "r", encoding="utf-8", errors="replace").read()
                lint_res = stata_mcp_analysis.lint_do_code(src_text, file_path=target)
                high_issues = [i for i in lint_res["issues"] if i["severity"] in ("error", "high")]
                if high_issues:
                    preview = "\n".join(
                        "  - L%d [%s] %s: %s (suggestion: %s)"
                        % (i["line"], i["severity"], i["rule_id"], i["message"], i["suggestion"])
                        for i in high_issues[:5]
                    )
                    lint_banner = "[PRE-RUN LINT WARNINGS (%s)]\n%s\n\n" % (lint_res["summary"], preview)
            except OSError:
                pass
        wait = cfg.sync_wait if wait_seconds is None or wait_seconds < 0 else wait_seconds
        job = jobs.start("file", target, working_dir, session_id, timeout, source_file=target, line_offset=0)
        job.wait(min(max(wait, 0.0), MAX_SYNC_WAIT))
        if job.status == RUNNING:
            payload = _wait_hint(job)
            if lint_banner:
                payload["lint_warnings"] = lint_banner.strip()
            return _dumps(payload)
        body = job.result_text()
        return "%s>>> %s (job %s, %s in %.1fs)\n%s" % (
            lint_banner,
            target,
            job.job_id,
            job.status,
            job.elapsed(),
            body,
        )

    @mcp.tool(
        name="stata_run_selection",
        description=(
            "Run a piece of Stata code. Same asynchronous behaviour as stata_run_file: "
            "long selections return a job_id that you follow with stata_job_tail. Failures include [ERROR LOCATION]."
        ),
    )
    def stata_run_selection(
        selection: str,
        timeout: float = 0,
        working_dir: str = "",
        session_id: str = "",
        wait_seconds: float = -1,
    ) -> str:
        code = selection or ""
        if not code.strip():
            return "Error: selection is empty"
        staging = cfg.selection_dir
        os.makedirs(staging, exist_ok=True)
        path = os.path.join(staging, "selection-%s.do" % uuid.uuid4().hex[:8])
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(code if code.endswith("\n") else code + "\n")
        except OSError as exc:
            return "Error: cannot stage the selection: %s" % exc
        wait = cfg.sync_wait if wait_seconds is None or wait_seconds < 0 else wait_seconds
        job = jobs.start("selection", path, working_dir, session_id, timeout, source_file=path, line_offset=0)
        job.wait(min(max(wait, 0.0), MAX_SYNC_WAIT))
        if job.status == RUNNING:
            return _dumps(_wait_hint(job))
        body = job.result_text()
        return ">>> selection (job %s, %s in %.1fs)\n%s" % (job.job_id, job.status, job.elapsed(), body)

    @mcp.tool(
        name="stata_run_lines",
        description=(
            "Run lines [start_line, end_line] (1-based inclusive) of a .do file. Useful for fast "
            "iterative debugging after fixing an error; error line numbers map back to the original file."
        ),
    )
    def stata_run_lines(
        file_path: str,
        start_line: int,
        end_line: int,
        timeout: float = 0,
        working_dir: str = "",
        session_id: str = "",
        wait_seconds: float = -1,
    ) -> str:
        target = os.path.abspath(os.path.expanduser(file_path or ""))
        if not target or not os.path.isfile(target):
            return "Error: file not found: %s" % (file_path or "<empty>")
        try:
            with open(target, "r", encoding="utf-8", errors="replace") as handle:
                all_lines = handle.read().splitlines()
        except OSError as exc:
            return "Error: cannot read %s: %s" % (target, exc)
        total = len(all_lines)
        if total == 0:
            return "Error: %s is empty" % target
        s_line = int(start_line)
        e_line = int(end_line)
        if s_line < 1 or e_line < s_line or s_line > total:
            return "Error: invalid line range %d..%d for %s (%d lines)" % (s_line, e_line, target, total)
        e_line = min(e_line, total)
        snippet = "\n".join(all_lines[s_line - 1 : e_line]) + "\n"
        staging = cfg.selection_dir
        os.makedirs(staging, exist_ok=True)
        staged = os.path.join(staging, "lines-%d-%d-%s.do" % (s_line, e_line, uuid.uuid4().hex[:8]))
        try:
            with open(staged, "w", encoding="utf-8") as handle:
                handle.write(snippet)
        except OSError as exc:
            return "Error: cannot stage line slice: %s" % exc
        wait = cfg.sync_wait if wait_seconds is None or wait_seconds < 0 else wait_seconds
        job = jobs.start(
            "lines",
            staged,
            working_dir,
            session_id,
            timeout,
            source_file=target,
            line_offset=s_line - 1,
        )
        job.wait(min(max(wait, 0.0), MAX_SYNC_WAIT))
        if job.status == RUNNING:
            return _dumps(_wait_hint(job))
        body = job.result_text()
        return ">>> %s:%d-%d (job %s, %s in %.1fs)\n%s" % (
            target,
            s_line,
            e_line,
            job.job_id,
            job.status,
            job.elapsed(),
            body,
        )

    @mcp.tool(
        name="stata_job_status",
        description=(
            "Status of one Stata job: running/finished/failed/timed_out/cancelled, elapsed time, "
            "current step, error_code/error_line if failed, and log record count. wait_seconds blocks up to 25s."
        ),
    )
    def stata_job_status(job_id: str, wait_seconds: float = 0) -> str:
        job = jobs.get(job_id)
        if job is None:
            return "Error: unknown job %r (use stata_job_list to see recent jobs)" % job_id
        return _dumps(job.summary(wait_seconds))

    @mcp.tool(
        name="stata_job_tail",
        description=(
            "Read the steps recorded by a Stata job since offset. Default mode 'steps' returns "
            "one entry per Stata command with a short output preview; mode 'full' returns raw log "
            "lines. Use next_offset from the reply as the next offset."
        ),
    )
    def stata_job_tail(job_id: str, offset: int = 0, limit: int = 200, mode: str = "steps") -> str:
        job = jobs.get(job_id)
        if job is None:
            return "Error: unknown job %r (use stata_job_list to see recent jobs)" % job_id
        payload = _tail_payload(job, offset, limit, mode if mode in ("steps", "full") else "steps")
        payload["journal"] = getattr(job, "journal_path", "")
        return _dumps(payload)

    @mcp.tool(
        name="stata_job_list",
        description="List recent Stata jobs with their status, step number, error_line (if failed) and elapsed time.",
    )
    def stata_job_list(limit: int = 20) -> str:
        return _dumps({"jobs": jobs.list(limit=limit)})

    @mcp.tool(
        name="stata_job_cancel",
        description="Stop a running Stata job (sends the engine's stop request) and report its status.",
    )
    def stata_job_cancel(job_id: str) -> str:
        job = jobs.get(job_id)
        if job is None:
            return "Error: unknown job %r" % job_id
        if job.status != RUNNING:
            return _dumps(job.summary())
        if isinstance(job, Job):
            job._cancel_requested = True
        try:
            result = engine_stop(cfg, getattr(job, "session_id", ""))
        except EngineError as exc:
            return "Error: %s" % exc
        deadline = time.time() + MAX_SYNC_WAIT
        while time.time() < deadline and job.status == RUNNING:
            time.sleep(0.2)
        if job.status == RUNNING and isinstance(job, Job):
            job._finish(CANCELLED, "stop requested by the client")
        payload = job.summary()
        payload["engine"] = result
        return _dumps(payload)

    @mcp.tool(
        name="stata_session",
        description=(
            "Manage Stata sessions (upstream passthrough). action='list' lists sessions, "
            "action='destroy' with session_id stops one. Parallel jobs need distinct session ids."
        ),
    )
    def stata_session(action: str = "list", session_id: str = "") -> str:
        parameters = {"action": action or "list"}
        if session_id:
            parameters["session_id"] = session_id
        try:
            payload = engine_post_json(cfg, "/v1/tools", {"tool": "stata_session", "parameters": parameters}, timeout=60.0)
        except EngineError as exc:
            return "Error: %s" % exc
        if payload.get("status") == "success":
            return str(payload.get("result", ""))
        return "Error: %s" % payload.get("message", "unknown engine error")

    @mcp.tool(
        name="stata_help",
        description=(
            "Read official Stata / installed SSC help documentation before writing commands so options and "
            "syntax are exact. Automatically resolves aliases (e.g. 'reg' -> 'regress'). Use section "
            "('syntax', 'options', 'examples', 'results', 'description') to retrieve a focused section."
        ),
    )
    def stata_help(topic: str, section: str = "", max_chars: int = 16000) -> str:
        if not topic or not topic.strip():
            return "Error: topic is required"
        local = stata_mcp_analysis.resolve_help(
            topic,
            stata_path=cfg.stata_path,
            section=section,
            max_chars=max_chars,
        )
        if local.get("found"):
            header = "Help: %s (resolved: %s, source: %s)" % (
                local["topic"],
                local["resolved_topic"],
                local.get("source", "local"),
            )
            sections_line = "Sections: " + ", ".join(local.get("sections_available") or [])
            see_line = ("Also see: " + ", ".join(local["also_see"][:8])) if local.get("also_see") else ""
            parts = [header, sections_line]
            if see_line:
                parts.append(see_line)
            parts.append("-" * 60)
            parts.append(local["content"])
            return "\n".join(parts)

        # Fallback to upstream engine /help endpoint for adopath locations outside standard dirs
        clean = re.sub(r"\s+", "_", topic.strip().lstrip("#").split(",")[0].strip())
        if re.match(r"^[A-Za-z0-9_.\-]+$", clean):
            try:
                status, body = engine_get_text(cfg, "/help?" + urlencode({"topic": clean, "format": "text"}), timeout=30.0)
                if status == 200 and body.strip():
                    limit = max(500, min(int(max_chars or 16000), 60000))
                    if len(body) > limit:
                        body = body[:limit] + "\n\n… (truncated)"
                    return "Help: %s (source: engine /help)\n%s\n%s" % (clean, "-" * 60, body)
            except EngineError:
                pass
        return "Help not found for %r. Try `stata_search_help(query=%r)`." % (topic, topic)

    @mcp.tool(
        name="stata_search_help",
        description="Search installed Stata base and SSC/plus help files and command aliases by keyword.",
    )
    def stata_search_help(query: str, limit: int = 20) -> str:
        return _dumps(stata_mcp_analysis.search_help(query, stata_path=cfg.stata_path, limit=limit))

    @mcp.tool(
        name="stata_which",
        description=(
            "Check whether a Stata command (built-in or SSC/ado package) is installed, its file path, "
            "and its version header."
        ),
    )
    def stata_which(command: str, session_id: str = "") -> str:
        cmd = (command or "").strip().split()[0] if command else ""
        if not cmd or not re.match(r"^[A-Za-z0-9_]+$", cmd):
            return "Error: command must be a valid Stata identifier (letters, digits, underscore)"
        local = stata_mcp_analysis.which_local(cmd, stata_path=cfg.stata_path)
        live_output = ""
        try:
            job = _run_quick_selection(jobs, cfg, "capture noisily which %s" % cmd, session_id=session_id, timeout=15.0)
            if job.status != RUNNING:
                live_output = job.result_text()
        except Exception:
            live_output = ""
        installed = local.get("found") or ("built-in" in live_output.lower()) or (".ado" in live_output.lower())
        payload = {
            "command": cmd,
            "installed": bool(installed),
            "local_lookup": local,
            "stata_which_output": live_output,
        }
        if not installed:
            payload["hint"] = "Not installed. Try `ssc install %s` or `search %s`." % (cmd, cmd)
        return _dumps(payload)

    @mcp.tool(
        name="stata_diagnose_error",
        description=(
            "Pinpoint the exact 1-based line number, source context snippet, error code explanation, and "
            "fix hint for a failed Stata job (or raw file_path + error_log). Leave job_id empty to inspect "
            "the most recent failed job."
        ),
    )
    def stata_diagnose_error(job_id: str = "", file_path: str = "", error_log: str = "") -> str:
        if file_path and error_log:
            target = os.path.abspath(os.path.expanduser(file_path))
            src = ""
            if os.path.isfile(target):
                try:
                    with open(target, "r", encoding="utf-8", errors="replace") as handle:
                        src = handle.read()
                except OSError:
                    src = ""
            diag = stata_mcp_analysis.locate_error_in_do(src, [], file_path=target, error_log=error_log)
            if not diag:
                return _dumps({"found": False, "message": "No Stata r(NNN) error found in error_log."})
            return _dumps({"found": True, "diagnosis": diag, "formatted": stata_mcp_analysis.format_error_diagnosis(diag)})

        job = None
        if job_id:
            job = jobs.get(job_id)
            if job is None:
                return "Error: unknown job %r" % job_id
        else:
            for row in jobs.list(limit=30):
                if row.get("status") == FAILED:
                    job = jobs.get(row["job_id"])
                    break
            if job is None:
                recent = jobs.list(limit=1)
                if recent:
                    job = jobs.get(recent[0]["job_id"])
        if job is None:
            return "Error: no jobs recorded yet"

        diag = getattr(job, "error_diagnosis", None)
        if diag is None:
            src_path = getattr(job, "source_file", "") or getattr(job, "file_path", "")
            src_text = ""
            if src_path and os.path.isfile(src_path):
                try:
                    with open(src_path, "r", encoding="utf-8", errors="replace") as handle:
                        src_text = handle.read()
                except OSError:
                    src_text = ""
            diag = stata_mcp_analysis.locate_error_in_do(
                src_text,
                job.read_records(),
                file_path=src_path,
                line_offset=getattr(job, "line_offset", 0),
            )
        if not diag:
            return _dumps({"job_id": job.job_id, "status": job.status, "found": False, "message": "No fatal Stata error detected in this job."})
        return _dumps(
            {
                "job_id": job.job_id,
                "status": job.status,
                "found": True,
                "diagnosis": diag,
                "formatted": stata_mcp_analysis.format_error_diagnosis(diag),
            }
        )

    @mcp.tool(
        name="stata_lint_do",
        description=(
            "Static analyzer to prevent AI from writing slow/unidiomatic Stata loops (1/_N, levelsof+foreach, "
            "manual dummies, preserve/restore in loops, nested loops) and catch missing-value `>` traps or "
            "unbalanced braces/quotes. Pass either file_path or code."
        ),
    )
    def stata_lint_do(file_path: str = "", code: str = "", strict: bool = False) -> str:
        target = ""
        text = code or ""
        if file_path:
            target = os.path.abspath(os.path.expanduser(file_path))
            if not os.path.isfile(target):
                return "Error: file not found: %s" % file_path
            try:
                with open(target, "r", encoding="utf-8", errors="replace") as handle:
                    text = handle.read()
            except OSError as exc:
                return "Error: cannot read %s: %s" % (target, exc)
        if not text.strip():
            return "Error: provide either file_path or non-empty code"
        return _dumps(stata_mcp_analysis.lint_do_code(text, file_path=target, strict=bool(strict)))

    @mcp.tool(
        name="stata_vectorize_guide",
        description=(
            "Anti-loop vectorization advisor: returns idiomatic Stata alternatives (bysort, egen, collapse, "
            "i.var, L./D., frames, mata) for slow loop patterns. Pass `code` to auto-detect patterns in your "
            "script, or `pattern` ('obs_loop', 'group_agg', 'lag_lead_diff', 'rolling_window', 'dummies_interactions', "
            "'row_stats', 'group_regression', 'recode_inlist', 'bulk_rename', 'frames_not_preserve', 'mata_matrix', 'all')."
        ),
    )
    def stata_vectorize_guide(pattern: str = "", code: str = "") -> str:
        return _dumps(stata_mcp_analysis.vectorize_guide(pattern=pattern, code=code))

    @mcp.tool(
        name="stata_inspect_data",
        description=(
            "Inspect the dataset currently loaded in Stata memory (rows, columns, dtypes, and sample table) "
            "with optional Stata `if_condition`."
        ),
    )
    def stata_inspect_data(if_condition: str = "", max_rows: int = 20, session_id: str = "") -> str:
        limit = max(1, min(int(max_rows or 20), 200))
        params = {"max_rows": limit}
        if if_condition and if_condition.strip():
            params["if_condition"] = if_condition.strip()
        if session_id and session_id.strip():
            params["session_id"] = session_id.strip()
        try:
            payload = engine_get_json(cfg, "/view_data?" + urlencode(params), timeout=30.0)
        except EngineError as exc:
            return "Error: %s" % exc
        if payload.get("status") != "success":
            return "Error: %s" % payload.get("message", "could not inspect data")
        columns = payload.get("columns") or []
        rows = payload.get("data") or []
        preview = _format_table_preview(columns, rows, max_rows=limit)
        return _dumps(
            {
                "total_rows": payload.get("total_rows", payload.get("rows", len(rows))),
                "displayed_rows": len(rows),
                "columns": columns,
                "dtypes": payload.get("dtypes", {}),
                "table_preview": preview,
            }
        )

    @mcp.tool(
        name="stata_describe",
        description=(
            "Run read-only `describe`, `summarize` (optional `detail=True`), and `misstable summarize` on the "
            "dataset in memory (or a specified `varlist`) to verify exact variable names, types, and missingness."
        ),
    )
    def stata_describe(varlist: str = "", detail: bool = False, session_id: str = "") -> str:
        vl = (varlist or "").strip()
        if vl and re.search(r'[;\r\n`"$]', vl):
            return "Error: varlist contains disallowed characters"
        target_vars = (" " + vl) if vl else ""
        sum_opt = ", detail" if detail else ""
        code = "\n".join(
            [
                "describe%s" % target_vars,
                "summarize%s%s" % (target_vars, sum_opt),
                "capture noisily misstable summarize%s" % target_vars,
            ]
        )
        job = _run_quick_selection(jobs, cfg, code, session_id=session_id, timeout=25.0)
        if job.status == RUNNING:
            return _dumps(_wait_hint(job))
        return job.result_text()

    @mcp.tool(
        name="stata_stored_results",
        description=(
            "Inspect Stata stored results currently in memory: kind='all' | 'e' (estimation) | "
            "'r' (general) | 'c' (system/dataset metadata) | 'estimates' (saved models)."
        ),
    )
    def stata_stored_results(kind: str = "all", session_id: str = "") -> str:
        k = (kind or "all").strip().lower()
        if k not in ("all", "e", "r", "c", "estimates"):
            return "Error: kind must be one of all, e, r, c, estimates"
        snippets = []
        if k in ("all", "e"):
            snippets.append("capture noisily ereturn list")
        if k in ("all", "r"):
            snippets.append("capture noisily return list")
        if k in ("all", "estimates"):
            snippets.append("capture noisily estimates dir")
        if k in ("all", "c"):
            snippets.append(
                'display "c(N)=" c(N) "  c(k)=" c(k) "  c(stata_version)=" c(stata_version) '
                '"  c(pwd)=" `"`c(pwd)\'"\' "  c(filename)=" `"`c(filename)\'"\''
            )
        job = _run_quick_selection(jobs, cfg, "\n".join(snippets), session_id=session_id, timeout=20.0)
        if job.status == RUNNING:
            return _dumps(_wait_hint(job))
        return job.result_text()

    return mcp


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------
def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="stata-mcp-jobs",
        description="Local MCP gateway with asynchronous Stata jobs and per-step journals.",
    )
    parser.add_argument("--home", help="state directory (default: ~/.stata-mcp or STATA_MCP_HOME)")
    parser.add_argument("--host", help="bind host (default from state.json, else 127.0.0.1)")
    parser.add_argument("--port", type=int, help="client-facing port (default from state.json, else 4001)")
    parser.add_argument("--engine-port", type=int, help="upstream engine port (default: state.json or port+1)")
    parser.add_argument("--default-timeout", type=float, help="default job timeout in seconds (default: 21600)")
    parser.add_argument("--sync-wait", type=float, help="seconds a synchronous tool call waits (default: 20, max 25)")
    parser.add_argument("--log-file", help="write gateway logs to this file")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    cfg = Config(args)
    if args.log_file:
        os.makedirs(os.path.dirname(os.path.abspath(args.log_file)) or ".", exist_ok=True)
        logging.basicConfig(
            filename=args.log_file,
            level=logging.INFO,
            format="%(asctime)s - %(levelname)s - %(message)s",
        )
    else:
        logging.basicConfig(level=logging.WARNING, format="%(asctime)s - %(levelname)s - %(message)s")

    logging.info("starting stata-mcp gateway (%s)", cfg.describe())
    server = build_server(cfg)
    try:
        server.run(transport="streamable-http")
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
