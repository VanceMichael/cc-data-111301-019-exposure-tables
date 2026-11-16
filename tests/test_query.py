"""曝光查询测试：候选与质量依据、回溯与部分缺口可见性。"""

from pathlib import Path
import tempfile
import unittest

from exposure_tables.manifest import Registry
from exposure_tables.query import query_exposure
from tests import fits_builder


class QueryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.registry = Registry(self.root / 'work')
        self.full = fits_builder.build_complete(
            self.root / 'full.fits', obsid='exp-q')
        self.registry.ingest_delivery(self.full, 'exp-q', release=1)
        self.partial = fits_builder.build_quality_only(
            self.root / 'quality.fits', obsid='exp-q')
        self.registry.ingest_delivery(
            self.partial, 'exp-q', release=2, partial=True)
        self.claim = self.registry.claim('exp-q')

    def tearDown(self):
        self.directory.cleanup()

    def test_query_returns_candidates_with_quality_basis(self):
        view = query_exposure(self.registry, 'exp-q')
        self.assertEqual(view['claim_id'], self.claim['claim_id'])
        self.assertTrue(view['complete'])
        basis = view['quality_basis']
        self.assertEqual(basis['detection_qualver'], 1)
        self.assertTrue(basis['matched'])
        # 质量依据可反查到部分补交的 release 2
        self.assertEqual(basis['bound_quality']['release'], 2)
        self.assertEqual(basis['bound_quality']['extver'], 1)
        # 表按完整槽位身份建键，不被同名扩展覆盖
        self.assertIn('DETECTIONS[1]', view['tables'])
        self.assertIn('QUALITY[1]', view['tables'])

    def test_rows_trace_to_hdu_row_and_archived_file(self):
        view = query_exposure(self.registry, 'exp-q')
        detection = view['tables']['DETECTIONS[1]']
        cell = detection['rows'][0]['ID']
        provenance = cell['provenance']
        self.assertEqual(provenance['hdu_index'], 1)
        self.assertEqual(provenance['row'], 0)
        # 指向内容寻址归档对象，而非原始文件名
        self.assertTrue(provenance['file'].endswith(
            detection['metadata']['sha256']))

    def test_invalid_value_surfaces_as_null_not_zero(self):
        view = query_exposure(self.registry, 'exp-q')
        signal = [r['SIGNAL'] for r in
                  view['tables']['DETECTIONS[1]']['rows']]
        self.assertIsNone(signal[1]['physical'])
        self.assertFalse(signal[1]['valid'])

    def test_row_windowing_is_honored(self):
        view = query_exposure(self.registry, 'exp-q',
                              row_start=1, row_limit=1)
        detection = view['tables']['DETECTIONS[1]']
        self.assertEqual(detection['row_start'], 1)
        self.assertEqual(detection['returned_rows'], 1)
        self.assertEqual(detection['total_rows'], 3)
        self.assertEqual(detection['rows'][0]['ID']['provenance']['row'], 1)

    def test_partial_gap_remains_visible_in_exposure_status(self):
        # 另一个只有探测表、缺质量表的曝光（独立目录避免 release 冲突）
        gap_root = Path(self.directory.name) / 'gap'
        gap_root.mkdir(parents=True, exist_ok=True)
        gap_registry = Registry(gap_root / 'work')
        det = fits_builder.build_detections_only(
            gap_root / 'det.fits', obsid='exp-gap')
        gap_registry.ingest_delivery(
            det, 'exp-gap', release=1, partial=True)
        status = gap_registry.exposure_status('exp-gap')
        self.assertFalse(status['complete'])
        self.assertEqual(status['missing'][0]['extname'], 'QUALITY')
        view = query_exposure(gap_registry, 'exp-gap')
        # 没有完整领取时不编造数据，明确给出状态与缺口
        self.assertEqual(view['status'], 'no_complete_claim')
        self.assertIn('QUALITY', [m['extname'] for m in view['missing']])

    def test_historical_claim_remains_queryable(self):
        # 再来一次领取后，旧 claim_id 仍可显式回溯
        new_claim = self.registry.claim('exp-q')
        old_view = query_exposure(
            self.registry, 'exp-q', claim_id=self.claim['claim_id'])
        self.assertEqual(old_view['claim_id'], self.claim['claim_id'])
        self.assertNotEqual(old_view['claim_id'], new_claim['claim_id'])

    def test_quality_mask_bits_are_exposed(self):
        view = query_exposure(self.registry, 'exp-q')
        mask = view['tables']['QUALITY[1]']['rows'][0]['MASK']
        self.assertEqual(len(mask['physical']), 8)
        # 部分补交第一行点亮第 0 位（MSB-first 的最高有效位）
        self.assertTrue(mask['physical'][0])
        self.assertFalse(any(mask['physical'][1:]))

    def test_multiple_quality_versions_are_not_collapsed(self):
        # 在同一曝光下再补交一个 EXTVER=2 的质量掩码
        newer = fits_builder.build_quality_only(
            self.root / 'quality-v2.fits', obsid='exp-q', extver=2)
        self.registry.ingest_delivery(
            newer, 'exp-q', release=3, partial=True)
        self.registry.claim('exp-q')
        view = query_exposure(self.registry, 'exp-q')
        # 两个质量槽位都保留，没有因同名而互相覆盖
        self.assertIn('QUALITY[1]', view['tables'])
        self.assertIn('QUALITY[2]', view['tables'])
        basis = view['quality_basis']
        # 探测表声明 QUALVER=1，绑定的仍是旧版本；新版本只是候选
        self.assertEqual(basis['bound_quality']['extver'], 1)
        self.assertEqual(
            sorted(c['extver'] for c in basis['quality_candidates']),
            [1, 2])


if __name__ == '__main__':
    unittest.main()
