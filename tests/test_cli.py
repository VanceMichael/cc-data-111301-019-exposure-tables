import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class CommandTests(unittest.TestCase):
    def test_preview_and_raw_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            process = subprocess.run([sys.executable,'-m','exposure_tables','demo'],capture_output=True,text=True,timeout=15)
            self.assertEqual(process.returncode,0,process.stderr)
            result = json.loads(process.stdout)
            self.assertIn('资料预览',result)
            self.assertGreater(result['原始资料']['bytes'],0)
            self.assertEqual(len(result['原始资料']['sha256']),64)

    def test_manifest_reports_incomplete(self):
        process = subprocess.run(
            [sys.executable, '-m', 'exposure_tables', 'manifest',
             'fixtures/quality-only.fits'],
            capture_output=True, text=True, timeout=15)
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(process.stdout)
        self.assertFalse(result['complete'])
        self.assertIn('missing_table',
                      [item['code'] for item in result['missing']])

    def test_receive_confirm_claim_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            store = str(Path(directory) / 'store')
            receive = subprocess.run(
                [sys.executable, '-m', 'exposure_tables', 'receive',
                 'fixtures/exposure.fits', '--exposure', 'exposure-a',
                 '--release', '1', '--store', store],
                capture_output=True, text=True, timeout=15)
            self.assertEqual(receive.returncode, 0, receive.stderr)
            confirm = subprocess.run(
                [sys.executable, '-m', 'exposure_tables', 'confirm',
                 '--exposure', 'exposure-a', '--release', '1',
                 '--store', store],
                capture_output=True, text=True, timeout=15)
            self.assertEqual(confirm.returncode, 0, confirm.stderr)
            claim = subprocess.run(
                [sys.executable, '-m', 'exposure_tables', 'claim',
                 '--exposure', 'exposure-a', '--store', store,
                 '--start', '1', '--limit', '1', '--by', 'tester'],
                capture_output=True, text=True, timeout=15)
            self.assertEqual(claim.returncode, 0, claim.stderr)
            result = json.loads(claim.stdout)
            self.assertEqual(result['rows_read'], 1)
            self.assertEqual(result['rows'][0]['row'], 1)
            self.assertEqual(result['rows'][0]['quality']['row'], 1)

    def test_store_error_is_not_empty_success(self):
        with tempfile.TemporaryDirectory() as directory:
            process = subprocess.run(
                [sys.executable, '-m', 'exposure_tables', 'receive',
                 'fixtures/quality-only.fits', '--exposure', 'exposure-a',
                 '--partial', '--store', str(Path(directory) / 'store')],
                capture_output=True, text=True, timeout=15)
            self.assertNotEqual(process.returncode, 0)
            self.assertEqual(process.stdout.strip(), '')
            self.assertIn('错误', process.stderr)
