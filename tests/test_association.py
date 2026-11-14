import tempfile
import unittest
from pathlib import Path

import numpy as np
from astropy.io import fits

from exposure_tables.association import assess_exposure
from exposure_tables.manifest import describe_exposure


ROOT = Path(__file__).resolve().parent.parent
EXPOSURE = ROOT / 'fixtures' / 'exposure.fits'
QUALITY_ONLY = ROOT / 'fixtures' / 'quality-only.fits'


def entry(obsid, name, extver, rows=2, qualver=None, key=None, readable=True,
          hdu=None):
    return {
        'key': key or '{}|{}|{}'.format(obsid, name, extver),
        'identity': {'obsid': obsid, 'name': name, 'extver': extver,
                     'valid': True, 'key': key or '{}|{}|{}'.format(obsid, name, extver)},
        'hdu': hdu, 'rows': rows, 'readable': readable,
        'quality_version': qualver,
    }


class ManifestTests(unittest.TestCase):
    def test_manifest_uses_identity_not_hdu_position(self):
        manifest = describe_exposure(EXPOSURE, exposure='exposure-a')
        keys = [item['key'] for item in manifest['extensions']]
        self.assertEqual(keys, ['exposure-a|DETECTIONS|1', 'exposure-a|QUALITY|1'])
        self.assertEqual(manifest['hdus'][1]['hdu'], 1)
        self.assertEqual(manifest['hdus'][2]['hdu'], 2)
        self.assertTrue(manifest['complete'])
        self.assertEqual(manifest['missing'], [])

    def test_missing_table_is_visible_not_success(self):
        manifest = describe_exposure(QUALITY_ONLY, exposure='exposure-a')
        self.assertFalse(manifest['complete'])
        self.assertEqual(manifest['status'], 'incomplete')
        self.assertIn('missing_table',
                      [item['code'] for item in manifest['missing']])
        self.assertIn('missing_table',
                      [item['code'] for item in manifest['assessment']['missing']])

    def test_duplicate_identity_is_an_error(self):
        extensions = [entry('e', 'DETECTIONS', 1, qualver=1, hdu=1),
                      entry('e', 'DETECTIONS', 1, qualver=1, hdu=2),
                      entry('e', 'QUALITY', 1, hdu=3)]
        assessment = assess_exposure('e', extensions)
        self.assertFalse(assessment['complete'])
        self.assertIn('ambiguous_table',
                      [item['code'] for item in assessment['issues']])

    def test_qualver_selects_quality_version(self):
        extensions = [entry('e', 'DETECTIONS', 1, qualver=2, hdu=1),
                      entry('e', 'QUALITY', 1, hdu=2),
                      entry('e', 'QUALITY', 2, hdu=3),
                      entry('e', 'QUALITY', 2, hdu=4)]
        assessment = assess_exposure('e', extensions)
        self.assertFalse(assessment['complete'])
        self.assertIn('duplicate_quality_version',
                      [item['code'] for item in assessment['issues']])

    def test_missing_referenced_quality_version_is_missing(self):
        extensions = [entry('e', 'DETECTIONS', 1, qualver=7, hdu=1),
                      entry('e', 'QUALITY', 1, hdu=2)]
        assessment = assess_exposure('e', extensions)
        self.assertFalse(assessment['complete'])
        self.assertIn('missing_quality_version',
                      [item['code'] for item in assessment['missing']])

    def test_row_count_mismatch_is_not_truncated(self):
        extensions = [entry('e', 'DETECTIONS', 1, rows=2, qualver=1, hdu=1),
                      entry('e', 'QUALITY', 1, rows=1, hdu=2)]
        assessment = assess_exposure('e', extensions)
        self.assertFalse(assessment['complete'])
        self.assertIn('row_count_mismatch',
                      [item['code'] for item in assessment['issues']])

    def test_manifest_does_not_read_table_data(self):
        import builtins
        from unittest import mock
        from exposure_tables import fingerprint as fingerprint_module

        def forbidden(*args, **kwargs):
            raise AssertionError('清单不应读取数据区')

        with mock.patch.object(fingerprint_module, 'fingerprint_extension',
                               side_effect=forbidden):
            manifest = describe_exposure(EXPOSURE, exposure='exposure-a')
        self.assertTrue(manifest['complete'])
        self.assertNotIn('fingerprint', manifest['extensions'][0])

    def test_truncated_data_is_reported(self):
        source = EXPOSURE.read_bytes()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'truncated.fits'
            path.write_bytes(source[:5796])
            manifest = describe_exposure(path, exposure='exposure-a')
            self.assertFalse(manifest['complete'])
            first = manifest['extensions'][0]
            self.assertFalse(first['readable'])
            self.assertIn('truncated_data',
                          [item['code'] for item in first['issues']])


if __name__ == '__main__':
    unittest.main()
