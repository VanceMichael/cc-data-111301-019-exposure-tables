import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class CommandTests(unittest.TestCase):
    def run_cli(self, *arguments):
        return subprocess.run(
            [sys.executable, '-m', 'exposure_tables', *arguments],
            capture_output=True, text=True, timeout=30)

    def test_demo_runs_full_delivery_and_claim_flow(self):
        process = self.run_cli('demo')
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(process.stdout)
        self.assertIn('首次交付', result)
        self.assertIn('部分补交', result)
        self.assertIn('领取', result)
        self.assertIn('按需查询', result)
        self.assertEqual(result['部分补交']['status'], 'accepted')
        self.assertTrue(result['按需查询']['complete'])

    def test_inspect_reports_extension_identity(self):
        process = self.run_cli('inspect', 'fixtures/exposure.fits')
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(process.stdout)
        detection = next(h for h in result['hdus']
                         if h['name'] == 'DETECTIONS')
        self.assertEqual(detection['obsid'], 'exposure-a')
        self.assertEqual(detection['extver'], 1)
        self.assertEqual(detection['qualver'], 1)
        self.assertTrue(any(c['variable_length']
                            for c in detection['columns']))

    def test_deliver_status_claim_query_lifecycle(self):
        with tempfile.TemporaryDirectory() as directory:
            work = str(Path(directory) / 'work')
            deliver = self.run_cli(
                'deliver', 'fixtures/exposure.fits',
                '--exposure', 'exposure-a', '--release', '1',
                '--workdir', work)
            self.assertEqual(deliver.returncode, 0, deliver.stderr)
            self.assertEqual(json.loads(deliver.stdout)['status'],
                             'accepted')

            partial = self.run_cli(
                'deliver', 'fixtures/quality-only.fits',
                '--exposure', 'exposure-a', '--release', '2',
                '--partial', '--workdir', work)
            self.assertEqual(partial.returncode, 0, partial.stderr)

            status = self.run_cli('status', 'exposure-a',
                                  '--workdir', work)
            status_body = json.loads(status.stdout)
            self.assertTrue(status_body['complete'])
            self.assertEqual(status_body['missing'], [])

            claim = self.run_cli('claim', 'exposure-a', '--workdir', work)
            self.assertEqual(claim.returncode, 0, claim.stderr)
            self.assertEqual(json.loads(claim.stdout)['status'],
                             'complete')

            query = self.run_cli('query', 'exposure-a', '--workdir', work)
            self.assertEqual(query.returncode, 0, query.stderr)
            body = json.loads(query.stdout)
            self.assertIn('DETECTIONS[1]', body['tables'])
            self.assertIn('QUALITY[1]', body['tables'])

    def test_rejected_delivery_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as directory:
            work = str(Path(directory) / 'work')
            bad = Path(directory) / 'bad.fits'
            bad.write_bytes(b'not fits')
            process = self.run_cli(
                'deliver', str(bad), '--exposure', 'exp-x',
                '--workdir', work)
            self.assertNotEqual(process.returncode, 0)
            body = json.loads(process.stdout)
            self.assertEqual(body['status'], 'rejected')


if __name__ == '__main__':
    unittest.main()
