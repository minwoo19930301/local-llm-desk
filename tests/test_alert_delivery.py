"""Result visibility and notification completion must not duplicate a run."""
import json
import unittest
from unittest.mock import patch

from desk import jobs, runner
from tests.http_fixture import isolated_server


class DeliveryPersistence(unittest.TestCase):
    def test_result_and_once_completion_exist_before_notification(self):
        with isolated_server():
            job = jobs.create_job({'title': 'fixture', 'prompt': 'fixture', 'model': 'fixture-model:latest',
                                   'preset': 'once_5m', 'alert': 'always'})['job']
            ctx = runner.RunContext.from_job(job)
            record = runner._record(ctx, 'ok', task=runner.TaskResult(text='source-backed fixture'))
            receipt = {'macos': {'mode': 'window', 'submitted': True}}
            def notify(*args, **kwargs):
                self.assertEqual(runner.get_run(record['id'])['output'], 'source-backed fixture')
                self.assertFalse(jobs.get_job(job['id'])['enabled'])
                return receipt
            with patch.object(runner.alerts, 'notify', side_effect=notify) as notification:
                runner._finish(job, ctx, record)
            notification.assert_called_once()
            rows = runner.list_runs(job['id'])
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['alert'], receipt)
            self.assertEqual(json.loads((runner.RUNS_DIR / 'latest.json').read_text())['alert'], receipt)

    def test_receipt_does_not_replace_a_newer_jobs_latest_result(self):
        with isolated_server():
            first = runner._record(runner.RunContext(job_id='first', title='first', model='fixture', prompt='fixture'), 'ok')
            second = runner._record(runner.RunContext(job_id='second', title='second', model='fixture', prompt='fixture'), 'ok')
            runner._persist('first', first)
            runner._persist('second', second)
            first['alert'] = {'macos': {'expired': True}}
            runner._amend_alert('first', first)
            self.assertEqual(runner.get_run(first['id'])['alert'], first['alert'])
            self.assertEqual(json.loads((runner.RUNS_DIR / 'latest.json').read_text())['id'], second['id'])
            self.assertEqual(len(runner.list_runs('first')), 1)
            for invalid in ['../../secret-1', '/tmp/secret-1', 'first', 'first-../1']:
                self.assertIsNone(runner.get_run(invalid))


if __name__ == '__main__':
    unittest.main()
