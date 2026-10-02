"""
resource_budget.py - a process-wide ceiling on cached raster bytes, plus the
counters a production canary needs.

WHY
---
The Step-1 release audit (2026-09-22) raised a fair objection: BAND_CACHE_MAX_BYTES
capped ONE scene's arrays, not the total held at once. With 7 scenes per land and
10 workers the theoretical worst case was ~2.2 GB of cached rasters, and the claim
that this stays inside the runner was asserted rather than proven. The same
objection applies to Step-2 block reads, which are larger still.

This module makes the CACHE bound real instead of theoretical: every cached
array (data and mask) is charged against ONE shared budget, atomically, across
all worker threads. Be precise about what it is: a ceiling on the raster arrays
the pipeline deliberately HOLDS, not a ceiling on process RSS - NumPy
temporaries, GDAL's own block cache, PNG buffers and the Supabase client are
outside it. Peak RSS is therefore measured separately (peak_rss_mb) so the
canary sees the whole picture. When
the budget is exhausted the pipeline does not fail and does not degrade its
science - it simply stops caching and falls back to reading, which is exactly
the behaviour before any caching existed.

It also records what the audit asked a canary to measure: raster reads, cache
hits, read failures by class (including HTTP 429/5xx from Planetary Computer),
and peak cached bytes. main.py writes these into ndvi_run_summary.notes so a
4-worker baseline and a 10-worker run can be compared on evidence rather than
on elapsed time alone.
"""
from __future__ import annotations

import os
import threading
from typing import Dict

from logger import logger


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "")) if os.getenv(name) else default
    except ValueError:
        logger.warning(f"{name} is not an integer; using {default}")
        return default


# Total bytes of cached raster held at any instant by the whole process, across
# every worker. 384 MB leaves ample room beside NumPy temporaries, GDAL, the
# Supabase client and PNG buffers on a 7 GB ubuntu-latest runner.
RASTER_CACHE_BUDGET_BYTES = _env_int("RASTER_CACHE_BUDGET_BYTES", 384 * 1024 * 1024)


class ByteBudget:
    """Thread-safe accounting for cached bytes. Never raises."""

    def __init__(self, limit: int):
        self._limit = max(int(limit), 0)
        self._used = 0
        self._peak = 0
        self._denied = 0
        self._lock = threading.Lock()

    def acquire(self, nbytes: int) -> bool:
        """Reserve nbytes if the process-wide budget allows it."""
        n = max(int(nbytes), 0)
        with self._lock:
            if self._used + n > self._limit:
                self._denied += 1
                return False
            self._used += n
            self._peak = max(self._peak, self._used)
            return True

    def release(self, nbytes: int) -> None:
        n = max(int(nbytes), 0)
        with self._lock:
            self._used = max(self._used - n, 0)

    @property
    def peak_bytes(self) -> int:
        with self._lock:
            return self._peak

    @property
    def denied(self) -> int:
        with self._lock:
            return self._denied

    @property
    def limit(self) -> int:
        return self._limit


RASTER_CACHE = ByteBudget(RASTER_CACHE_BUDGET_BYTES)


class Counters:
    """Run-level counters for the canary comparison."""

    def __init__(self):
        self._lock = threading.Lock()
        self._c: Dict[str, int] = {}

    def bump(self, key: str, n: int = 1) -> None:
        with self._lock:
            self._c[key] = self._c.get(key, 0) + n

    def classify_read_error(self, exc: BaseException) -> None:
        """Count a failed raster read by class, so throttling is visible."""
        text = f"{type(exc).__name__}: {exc}"
        low = text.lower()
        if "429" in low or "too many requests" in low or "throttl" in low or "rate limit" in low:
            self.bump("read_errors_429")
        elif any(code in low for code in ("500", "502", "503", "504")) or "timeout" in low or "timed out" in low:
            self.bump("read_errors_5xx_or_timeout")
        else:
            self.bump("read_errors_other")

    def snapshot(self) -> Dict[str, int]:
        with self._lock:
            out = dict(self._c)
        out["raster_cache_peak_mb"] = round(RASTER_CACHE.peak_bytes / (1024 * 1024), 1)
        out["raster_cache_limit_mb"] = round(RASTER_CACHE.limit / (1024 * 1024), 1)
        out["raster_cache_denied"] = RASTER_CACHE.denied
        try:
            with open("/proc/self/status", "r") as fh:
                for line in fh:
                    if line.startswith("VmHWM:"):           # peak resident set
                        out["peak_rss_mb"] = round(int(line.split()[1]) / 1024, 1)
                        break
        except Exception:
            pass
        return out


COUNTERS = Counters()
