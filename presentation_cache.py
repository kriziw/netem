"""Nonblocking presentation reads; fresh control reads share one in-flight load."""
import copy
import threading
import time


class PresentationCache:
    def __init__(self, workers=4, clock=time.monotonic):
        self.lock = threading.Lock()
        self.capacity = threading.BoundedSemaphore(workers)
        self.entries = {}
        self.clock = clock

    def _entry(self, name, source):
        entry = self.entries.get(name)
        if entry is None or entry['source'] != source:
            entry = dict(source=source, value=None, checked=None, retry=0, loading=False,
                         done=threading.Event(), error=None, revision=0, duration_ms=None)
            self.entries[name] = entry
        return entry

    def _load(self, entry, loader):
        started = self.clock()
        try:
            value = loader()
            with self.lock:
                if entry['checked'] is None or entry['value'] != value:
                    entry['revision'] += 1
                entry.update(value=copy.deepcopy(value), checked=self.clock(), error=None)
            return value
        except Exception as exc:
            with self.lock:
                entry['error'] = exc
            raise
        finally:
            with self.lock:
                entry.update(loading=False, retry=self.clock() + 1, duration_ms=round((self.clock() - started) * 1000, 1))
                entry['done'].set()

    def fresh(self, name, source, loader):
        """Control/worker callers wait for a real load, coalescing concurrent reads."""
        with self.lock:
            entry = self._entry(name, source)
            wait = entry['loading']
            if not wait:
                entry['loading'] = True
                entry['done'].clear()
        if wait:
            entry['done'].wait()
            with self.lock:
                if entry['error'] is not None:
                    raise entry['error']
                return copy.deepcopy(entry['value'])
        return self._load(entry, loader)

    def read(self, name, source, loader, fallback, max_age=1, max_stale=3):
        """Return immediately; never use expired evidence or wait for another reader."""
        with self.lock:
            entry = self._entry(name, source)
            now = self.clock()
            age = None if entry['checked'] is None else now - entry['checked']
            if (age is None or age > max_age) and not entry['loading'] and now >= entry['retry'] and self.capacity.acquire(False):
                entry['loading'] = True
                entry['done'].clear()
                def poll():
                    try:
                        self._load(entry, loader)
                    except Exception:
                        pass  # Readers get unavailable state and a bounded retry, never stale live evidence.
                    finally:
                        self.capacity.release()
                threading.Thread(target=poll, name='netem-view-' + name, daemon=True).start()
            available = age is not None and 0 <= age <= max_stale
            value = copy.deepcopy(entry['value'] if available else fallback)
            if isinstance(value, dict):
                value['poll'] = dict(age_seconds=age, refreshing=entry['loading'], expired=not available, load_ms=entry['duration_ms'])
            return value

    def version(self, names):
        with self.lock:
            return [(name, self.entries[name]['source'], self.entries[name]['revision'])
                    for name in names if name in self.entries]
