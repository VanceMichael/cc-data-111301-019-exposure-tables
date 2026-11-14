import copy
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
from astropy.io import fits

from exposure_tables.store import ExposureStore, StoreError


ROOT = Path(__file__).resolve().parent.parent
EXPOSURE = ROOT / 'fixtures' / 'exposure.fits'
QUALITY_ONLY = ROOT / 'fixtures' / 'quality-only.fits'


def write_quality(path, rows=2, extver=1, bytes_row0=0x01, obsid='exposure-a'):
    array = np.array([[bytes_row0]] + [[0]] * (rows - 1), dtype=np.uint8)
    hdu = fits.BinTableHDU.from_columns(
        [fits.Column(name='MASK', format='8X', array=array)])
    hdu.name = 'QUALITY'
    hdu.header['EXTVER'] = extver
    hdu.header['OBSID'] = obsid
    fits.HDUList([fits.PrimaryHDU(), hdu]).writeto(path, overwrite=True)
    return path


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = ExposureStore(self.root / 'store')

    def accept(self, path, **kwargs):
        receipt = self.store.receive(path, exposure='exposure-a', **kwargs)
        self.store.confirm('exposure-a', receipt['release'])
        return receipt

    def test_receive_does_not_publish_until_confirmed(self):
        receipt = self.store.receive(EXPOSURE, exposure='exposure-a')
        self.assertEqual(receipt['status'], 'pending')
        self.assertEqual(self.store.status('exposure-a')['accepted'], [])
        with self.assertRaises(StoreError):
            self.store.claim('exposure-a')
        self.store.confirm('exposure-a', receipt['release'])
        self.assertEqual(self.store.status('exposure-a')['accepted'], [1])

    def test_claim_returns_quality_basis_and_source(self):
        self.accept(EXPOSURE)
        claim = self.store.claim('exposure-a', claimed_by='alice')
        self.assertEqual(claim['rows_read'], 2)
        first = claim['rows'][0]
        self.assertEqual(first['row'], 0)
        self.assertEqual(first['extension']['index'], 1)
        self.assertEqual(first['quality']['row'], 0)
        self.assertEqual(first['quality']['hdu'], 2)
        self.assertEqual(first['quality']['values']['MASK']['set_bits'], [7])
        self.assertEqual(first['source']['original_name'], 'exposure.fits')
        self.assertEqual(len(first['source']['object']), 64)
        self.assertTrue(claim['basis']['complete'])
        self.assertEqual(len(claim['basis']['extensions']), 2)

    def test_claim_range_does_not_read_whole_file(self):
        self.accept(EXPOSURE)
        claim = self.store.claim('exposure-a', start=1, limit=1)
        self.assertEqual(claim['rows_read'], 1)
        self.assertEqual(claim['rows'][0]['row'], 1)
        self.assertEqual(claim['rows'][0]['quality']['row'], 1)

    def test_partial_delivery_preserves_base(self):
        self.accept(EXPOSURE)
        updated = self.root / 'quality-v2.fits'
        write_quality(updated, bytes_row0=0x80)
        receipt = self.store.receive(updated, exposure='exposure-a',
                                     partial=True,
                                     note='质量表补交')
        self.assertEqual(receipt['delivery']['mode'], 'partial')
        self.assertEqual(receipt['delivery']['base_release'], 1)
        self.assertEqual(receipt['delivery']['replaces'],
                         {'exposure-a|QUALITY|1': 'exposure-a|QUALITY|1'})
        self.store.confirm('exposure-a', receipt['release'])
        # 旧版本仍可领取，且仍是旧的位
        old = self.store.claim('exposure-a', release=1)
        self.assertEqual(old['rows'][0]['quality']['values']['MASK']['set_bits'], [7])
        new = self.store.claim('exposure-a', release=2)
        self.assertEqual(new['rows'][0]['quality']['values']['MASK']['set_bits'], [0])

    def test_partial_cannot_break_a_complete_exposure(self):
        self.accept(EXPOSURE)
        broken = self.root / 'quality-short.fits'
        write_quality(broken, rows=1)
        with self.assertRaises(StoreError):
            self.store.receive(broken, exposure='exposure-a', partial=True)
        self.assertEqual(self.store.status('exposure-a')['pending'], [])

    def test_partial_rejects_undeclared_extensions(self):
        self.accept(EXPOSURE)
        extra = self.root / 'quality-2.fits'
        write_quality(extra, extver=2)
        with self.assertRaises(StoreError):
            self.store.receive(extra, exposure='exposure-a', partial=True)
        receipt = self.store.receive(
            extra, exposure='exposure-a', partial=True,
            adds=['exposure-a|QUALITY|2'])
        self.assertEqual(receipt['delivery']['adds'], ['exposure-a|QUALITY|2'])

    def test_incomplete_delivery_visible_in_query_and_blocks_claim(self):
        self.accept(QUALITY_ONLY)
        query = self.store.query('exposure-a')
        self.assertFalse(query['complete'])
        self.assertIn('missing_table', [item['code'] for item in query['missing']])
        with self.assertRaises(StoreError):
            self.store.claim('exposure-a')
        # 缺 DETECTIONS 时不能凭空领取该表，但可显式领取已有的 QUALITY
        with self.assertRaises(StoreError):
            self.store.claim('exposure-a', allow_incomplete=True)
        claim = self.store.claim('exposure-a', table='QUALITY',
                                 allow_incomplete=True)
        self.assertFalse(claim['basis']['complete'])
        self.assertIn('missing_table',
                      [item['code'] for item in claim['basis']['missing']])

    def test_add_missing_table_to_incomplete_exposure(self):
        self.accept(QUALITY_ONLY)
        receipt = self.store.receive(EXPOSURE, exposure='exposure-a',
                                     partial=True,
                                     adds=['exposure-a|DETECTIONS|1'])
        self.assertTrue(receipt['complete'])
        self.store.confirm('exposure-a', receipt['release'])
        claim = self.store.claim('exposure-a')
        self.assertEqual(claim['rows_read'], 2)
        self.assertTrue(claim['basis']['complete'])

    def test_corrupt_object_is_refused_at_claim(self):
        self.accept(EXPOSURE)
        receipt = self.store.read_receipt('exposure-a', 1)
        object_path = self.store.objects / receipt['source']['sha256']
        object_path.write_bytes(object_path.read_bytes() + b'x')
        with self.assertRaises(StoreError):
            self.store.claim('exposure-a')
        verify = self.store.verify('exposure-a')
        self.assertFalse(verify['valid'])

    def test_confirm_refuses_tampered_archive(self):
        receipt = self.store.receive(EXPOSURE, exposure='exposure-a')
        object_path = self.store.objects / receipt['source']['sha256']
        object_path.write_bytes(b'not the original')
        with self.assertRaises(StoreError):
            self.store.confirm('exposure-a', receipt['release'])

    def test_corrupt_datasum_is_refused_on_receive(self):
        raw = bytearray(EXPOSURE.read_bytes())
        raw[5760 + 16] ^= 0xFF
        path = self.root / 'corrupt.fits'
        path.write_bytes(raw)
        with self.assertRaises(StoreError):
            self.store.receive(path, exposure='exposure-a')

    def test_claim_is_recorded_for_traceability(self):
        self.accept(EXPOSURE)
        self.store.claim('exposure-a', claimed_by='bob', start=1, limit=1)
        claims = self.store.claims_for('exposure-a')
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0]['claimed_by'], 'bob')
        self.assertEqual(claims[0]['start'], 1)
        self.assertEqual(claims[0]['rows_read'], 1)

    def test_release_number_is_not_reused(self):
        self.accept(EXPOSURE)
        with self.assertRaises(StoreError):
            self.store.receive(EXPOSURE, exposure='exposure-a', release=1)

    def test_query_rows_can_be_loaded_on_demand(self):
        self.accept(EXPOSURE)
        query = self.store.query('exposure-a', include_rows=True, limit=1)
        self.assertEqual(len(query['rows']), 1)
        self.assertEqual(query['rows'][0]['quality']['values']['MASK']['set_bits'], [7])


if __name__ == '__main__':
    unittest.main()
