"""Retryable delivery of the active private brand to the executing agent."""
import hashlib
import json
import threading


class BrandingSync:
    def __init__(self, store, sender, stop):
        self.store, self.sender, self.stop = store, sender, stop
        self.lock = threading.Lock()
        self.thread = None
        self.state = {'synced': False, 'message': 'Branding has not been synchronized yet.'}

    def sync(self):
        with self.lock:
            try:
                payload = self.store.export_config()
                revision = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
                # Check the agent's revision too: reconnection/reinstallation must restore it.
                remote = self.sender('/api/v1/branding', timeout=3)
                if remote.get('revision') != revision:
                    self.sender('/api/v1/branding', method='PUT', payload=payload, timeout=10)
                self.state = {'synced': True, 'revision': revision, 'message': 'Active brand synchronized to the simulator.'}
            except (ValueError, OSError, RuntimeError) as exc:
                self.state = {'synced': False, 'message': 'Simulator branding pending; retrying automatically. ' + str(exc)}
            return dict(self.state)

    def start(self):
        if self.thread is None or not self.thread.is_alive():
            def worker():
                while not self.stop.is_set():
                    self.sync()
                    self.stop.wait(30)
            self.thread = threading.Thread(target=worker, name='netem-branding-sync', daemon=True)
            self.thread.start()
