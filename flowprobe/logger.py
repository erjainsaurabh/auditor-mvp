"""Shared logging configuration for flowprobe.

All modules import `get_logger(__name__)` — they do NOT call basicConfig themselves.
Bootstrap is done once in api.py / run.py entrypoints via `setup_logging()`.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import queue
import sys
import threading
from pathlib import Path


class _SeqHttpHandler(logging.Handler):
    """Thread-safe handler that ships JSON log records to Seq via HTTP.

    Uses a background daemon thread + queue so the calling thread never blocks
    on network I/O. Works correctly when log calls originate from
    ThreadPoolExecutor workers (unlike seqlog's internal consumer thread).
    """

    def __init__(self, server_url: str, api_key: str | None = None) -> None:
        super().__init__()
        self._url = server_url.rstrip("/") + "/api/events/raw?clef"
        self._headers = {"Content-Type": "application/vnd.serilog.clef"}
        if api_key:
            self._headers["X-Seq-ApiKey"] = api_key
        self._queue: queue.Queue = queue.Queue(maxsize=1000)
        self._thread = threading.Thread(target=self._worker, daemon=True, name="seq-http")
        self._thread.start()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._queue.put_nowait(record)
        except queue.Full:
            pass  # drop rather than block the caller

    def _worker(self) -> None:
        import urllib.request

        while True:
            record = self._queue.get()
            if record is None:
                self._queue.task_done()
                break
            try:
                payload = self._to_clef(record)
                data = (payload + "\n").encode("utf-8")
                req = urllib.request.Request(self._url, data=data, headers=self._headers, method="POST")
                urllib.request.urlopen(req, timeout=5)
            except Exception:
                pass  # never crash the worker
            finally:
                self._queue.task_done()

    _SKIP_FIELDS = frozenset({
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "message", "asctime", "taskName",
    })

    def _to_clef(self, record: logging.LogRecord) -> str:
        """Convert a LogRecord to Compact Log Event Format (CLEF) for Seq."""
        import datetime
        ts = datetime.datetime.fromtimestamp(record.created, tz=datetime.timezone.utc)
        doc: dict = {
            "@t": ts.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "@l": record.levelname,
            "@mt": record.getMessage(),
            "logger": record.name,
        }
        for k, v in record.__dict__.items():
            if k.startswith("_") or k in self._SKIP_FIELDS:
                continue
            try:
                json.dumps(v)
                doc[k] = v
            except (TypeError, ValueError):
                doc[k] = str(v)
        if record.exc_info:
            doc["@x"] = self.formatException(record.exc_info)
        return json.dumps(doc, default=str)

    def flush(self) -> None:
        # Drain remaining queue items before process exit
        self._queue.join()

    def close(self) -> None:
        self._queue.put(None)  # poison pill
        super().close()


def setup_logging(
    log_file: str | Path | None = "flowprobe.log",
    level: int = logging.DEBUG,
    seq_url: str | None = None,
) -> None:
    """Call once at process start. Safe to call multiple times."""
    root = logging.getLogger("flowprobe")
    if root.handlers:
        return  # already configured

    root.setLevel(level)

    try:
        from pythonjsonlogger import jsonlogger
        fmt = jsonlogger.JsonFormatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    except ImportError:
        fmt = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s — %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )

    # stderr (INFO+)
    sh = logging.StreamHandler(sys.stderr)
    sh.setLevel(logging.INFO)
    sh.setFormatter(fmt)
    root.addHandler(sh)

    # file (DEBUG+)
    if log_file:
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        root.addHandler(fh)

    # Seq (DEBUG+) — only if url provided at bootstrap time
    if seq_url:
        _attach_seq(root, seq_url)


def _attach_seq(root: logging.Logger, seq_url: str) -> None:
    """Add a Seq handler if not already present. Idempotent."""
    already = any(isinstance(h, _SeqHttpHandler) for h in root.handlers)
    if already:
        return
    try:
        h = _SeqHttpHandler(seq_url)
        h.setLevel(logging.DEBUG)
        root.addHandler(h)
        root.info("Seq handler connected — %s", seq_url,
                  extra={"event": "seq_connected", "seq_url": seq_url})
    except Exception as e:
        root.warning("Seq setup failed (%s) — continuing without Seq", e)


def setup_logging_from_config(config: dict, log_file: str | Path | None = None) -> None:
    """Wire Seq after config is loaded. Safe to call after setup_logging() already ran."""
    from flowprobe.config import settings
    seq_url = settings.seq_url or None
    file_override = log_file or settings.log_file

    root = logging.getLogger("flowprobe")
    if not root.handlers:
        setup_logging(log_file=file_override, seq_url=seq_url)
    elif seq_url:
        _attach_seq(root, seq_url)


def get_logger(name: str) -> logging.Logger:
    """Return a child logger under the 'flowprobe' hierarchy."""
    clean = name.removeprefix("flowprobe.") if name.startswith("flowprobe.") else name
    return logging.getLogger(f"flowprobe.{clean}")
