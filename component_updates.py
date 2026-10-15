"""Track one release check across NetEm and the simulator's serialized admin queue."""
import copy
import threading
import time


class ComponentChecks:
    def __init__(self, netem):
        self.netem = netem
        self.lock = threading.Lock()
        self.running = False
        self.components = {}

    def snapshot(self):
        with self.lock:
            return {'running': self.running, 'components': copy.deepcopy(self.components)}

    def set_state(self, component, state, message=''):
        with self.lock:
            self.components[component] = {'state': state, 'message': message}

    def connection(self):
        config = self.netem.traffic_generator_config(self.netem.load_config())
        return (config.get('host'), config.get('port'))

    def start(self):
        with self.lock:
            if self.running:
                return False
            self.running = True
            self.components = {name: {'state': 'checking', 'message': 'Checking for updates…'}
                               for name in ('netem', 'simulator', 'target')}
        try:
            threading.Thread(target=self.run, args=(self.connection(),), daemon=True).start()
        except Exception:
            with self.lock:
                self.running = False
            raise
        return True

    def read_agent(self, connection):
        if self.connection() != connection:
            raise RuntimeError('Simulator connection changed. Check for updates again.')
        state, error = self.netem.simulator_update_state()
        if error:
            raise RuntimeError(error)
        return state

    def wait_agent(self, connection, job_id=None, timeout=120):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.read_agent(connection)
            system = state.get('system') or {}
            job = system.get('job') or {}
            if job_id:
                if job.get('id') == job_id and job.get('state') in ('completed', 'failed'):
                    if job['state'] == 'failed':
                        raise RuntimeError(job.get('message') or 'Release check failed.')
                    return state
            elif not system.get('busy'):
                return state
            time.sleep(1)
        raise RuntimeError('Release check timed out. Check the agent task status before retrying.')

    def check_remote(self, name, action, connection):
        state = self.wait_agent(connection)
        if name == 'target' and not ((state.get('system') or {}).get('target') or {}).get('host'):
            raise RuntimeError('Connect the target management address under Settings → Agent & target first.')
        reply = self.netem.traffic_generator_request('/api/v1/controller/commands/system', method='POST',
                                                    payload={'action': action}, timeout=10)
        job_id = reply.get('job_id') or (reply.get('job') or {}).get('id')
        if not job_id:
            raise RuntimeError('The simulator did not identify the queued release check.')
        self.wait_agent(connection, job_id)

    def run(self, connection):
        try:
            try:
                status = self.netem.git_update_status(fetch=True)
                if not status.get('ok'):
                    raise RuntimeError(status.get('error') or 'NetEm release check failed.')
            except Exception as exc:
                self.set_state('netem', 'error', str(exc))
            else:
                self.set_state('netem', 'complete')
            # Both commands use the same simulator admin worker. Await the exact
            # first job before enqueuing the second; an old completed job is not enough.
            for name, action in (('simulator', 'check_update'), ('target', 'target_check')):
                try:
                    self.check_remote(name, action, connection)
                except Exception as exc:
                    self.set_state(name, 'error', str(exc))
                else:
                    self.set_state(name, 'complete')
        finally:
            with self.lock:
                self.running = False
