import unittest
from datetime import datetime, timedelta, timezone

from client_backend.journals import collector_alerts


class CollectorAlertsTest(unittest.TestCase):
    def test_blocked_state_warns_about_session_handoff(self):
        alerts = collector_alerts({'status': 'blocked', 'heartbeat_at': datetime.now(timezone.utc).isoformat()})
        self.assertEqual([a['code'] for a in alerts], ['collector_blocked'])

    def test_stale_heartbeat_warns_when_job_should_be_running(self):
        old = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        alerts = collector_alerts({'status': 'running', 'heartbeat_at': old})
        codes = [a['code'] for a in alerts]
        self.assertIn('collector_heartbeat_stale', codes)

    def test_stopped_job_with_old_heartbeat_is_quiet(self):
        old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        self.assertEqual(collector_alerts({'status': 'stopped', 'heartbeat_at': old}), [])

    def test_fresh_heartbeat_is_quiet(self):
        alerts = collector_alerts({'status': 'running', 'heartbeat_at': datetime.now(timezone.utc).isoformat()})
        self.assertEqual(alerts, [])

    def test_snapshot_marker_adds_info_alert(self):
        alerts = collector_alerts({'status': 'running', 'snapshot': True,
                                   'heartbeat_at': datetime.now(timezone.utc).isoformat()})
        self.assertEqual([a['code'] for a in alerts], ['collector_snapshot'])

    def test_non_dict_and_garbage_heartbeat_are_safe(self):
        self.assertEqual(collector_alerts(None), [])
        self.assertEqual(collector_alerts({'status': 'running', 'heartbeat_at': 'not-a-date'}), [])


if __name__ == '__main__':
    unittest.main()
