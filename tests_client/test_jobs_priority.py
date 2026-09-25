import tempfile
import threading
import time
import unittest
from pathlib import Path

from client_backend.service import Application


class JobPriorityTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = Application(Path(self.temp.name) / 'library')

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def wait_state(self, job_id, states, attempts=200):
        for _ in range(attempts):
            job = next(value for value in self.app.jobs.list() if value['id'] == job_id)
            if job['state'] in states:
                return job
            time.sleep(.02)
        self.fail(f'job {job_id} did not reach {states}')

    def test_interactive_job_preempts_queued_batch_work(self):
        gate = threading.Event()
        order = []
        self.app.jobs.handlers['test.slow'] = lambda p, progress: (gate.wait(10), order.append(p['tag']))[1]
        self.app.jobs.handlers['import'] = lambda p, progress: order.append('import') or {}
        self.app.jobs.handlers['fulltext.obtain'] = lambda p, progress: order.append('fulltext') or {}
        # Occupy all three workers with slow jobs, then queue batch + interactive work.
        blockers = [self.app.jobs.create('test.slow', {'tag': f'blocker-{index}'}) for index in range(3)]
        batch = self.app.jobs.create('import', {})
        interactive = self.app.jobs.create('fulltext.obtain', {})
        # Let exactly one worker free up; the interactive job must claim it first.
        time.sleep(.2)
        gate.set()
        self.wait_state(interactive['jobId'], ('completed',))
        self.wait_state(batch['jobId'], ('completed',))
        for job in blockers:
            self.wait_state(job['jobId'], ('completed',))
        self.assertLess(order.index('fulltext'), order.index('import'))

    def test_cancel_and_retry_roundtrip_still_works(self):
        gate = threading.Event()
        self.app.jobs.handlers['test.gated'] = lambda p, progress: (progress(.5, 'working'), gate.wait(10))[1]
        blockers = [self.app.jobs.create('test.gated', {'tag': index}) for index in range(3)]
        time.sleep(.2)
        target = self.app.jobs.create('test.gated', {'tag': 'target'})
        self.app.jobs.action(target['jobId'], 'cancel')
        gate.set()
        job = self.wait_state(target['jobId'], ('cancelled',))
        # A cancelled-but-still-queued job stays active until a worker drains it; retry after that.
        for _ in range(200):
            with self.app.jobs.lock:
                if target['jobId'] not in self.app.jobs.active:
                    break
            time.sleep(.02)
        self.app.jobs.action(target['jobId'], 'retry')
        self.assertEqual(self.wait_state(target['jobId'], ('completed',))['state'], 'completed')
        for job in blockers:
            self.wait_state(job['jobId'], ('completed',))


if __name__ == '__main__':
    unittest.main()
