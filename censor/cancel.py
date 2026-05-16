"""Cooperative cancellation for the censor pipeline.

The desktop GUI runs every job in a worker thread. Pressing the
Cancel button needs to be able to interrupt the worker:

* between pipeline stages, so the next stage simply doesn't start;
* during the long-running ffmpeg / yt-dlp subprocess calls, so we
  don't wait minutes for one of them to finish before honouring the
  click.

We expose a small :class:`CancelToken` for both. The token holds a
``threading.Event`` and a registry of in-flight subprocesses. Any code
spawning a kill-able subprocess can use the :func:`registered` helper
to register and auto-unregister around the call. Setting the token
flips the event AND terminates every still-registered subprocess.

The pipeline raises :class:`PipelineCancelled` once it observes the
flag, which the worker thread converts to a friendly status message.
"""

from __future__ import annotations

import contextlib
import subprocess
import threading
from typing import Iterator, Optional


class PipelineCancelled(RuntimeError):
    """Raised by the pipeline when the user has pressed Cancel."""


class CancelToken:
    """Thread-safe cancellation primitive.

    Code that wants to cooperate calls :meth:`raise_if_cancelled` at
    safe points. Code that spawns a subprocess wraps the spawn in
    :meth:`registered` so the subprocess will be terminated as soon as
    the token is set, even mid-encode.
    """

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._procs: list[subprocess.Popen] = []

    # ----- producer side --------------------------------------------------

    def cancel(self) -> None:
        """Mark the token cancelled and terminate every registered proc."""
        self._event.set()
        with self._lock:
            procs = list(self._procs)
            self._procs.clear()
        for p in procs:
            try:
                p.terminate()
            except Exception:  # noqa: BLE001
                pass

    def reset(self) -> None:
        """Clear the cancelled flag. Caller must ensure no procs are live."""
        self._event.clear()
        with self._lock:
            self._procs.clear()

    # ----- consumer side --------------------------------------------------

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise PipelineCancelled("Cancelled by user.")

    def register(self, proc: subprocess.Popen) -> None:
        """Track a subprocess so :meth:`cancel` can kill it. Safe to
        call after cancel() has already fired (we kill immediately)."""
        if self._event.is_set():
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass
            return
        with self._lock:
            self._procs.append(proc)

    def unregister(self, proc: subprocess.Popen) -> None:
        with self._lock:
            try:
                self._procs.remove(proc)
            except ValueError:
                pass

    @contextlib.contextmanager
    def registered(
        self, proc: subprocess.Popen
    ) -> Iterator[subprocess.Popen]:
        """Context manager that registers ``proc`` for the duration of
        the ``with`` block, then unregisters on exit."""
        self.register(proc)
        try:
            yield proc
        finally:
            self.unregister(proc)


# Module-level "current pipeline run" token. The pipeline pulls from
# here so layers underneath (audio.py / download.py / extractors.py)
# can spawn subprocesses without threading the token through every
# call. The pipeline sets/clears this around its run() body.
_active_token: Optional[CancelToken] = None
_active_lock = threading.Lock()


def set_active(token: Optional[CancelToken]) -> None:
    global _active_token  # noqa: PLW0603
    with _active_lock:
        _active_token = token


def get_active() -> Optional[CancelToken]:
    with _active_lock:
        return _active_token


def raise_if_cancelled() -> None:
    """Convenience: only raises if there's an active token AND it's set.

    Library code (audio renderer, download adapter, extractor chain)
    can call this between subprocess spawns without having to pass a
    token explicitly through every API."""
    tok = get_active()
    if tok is not None and tok.cancelled:
        raise PipelineCancelled("Cancelled by user.")
