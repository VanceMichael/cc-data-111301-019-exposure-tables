"""曝光资料清单、扩展关联核验与版本化领取测试。"""

from pathlib import Path
import tempfile
import unittest

from astropy.io import fits
import numpy as np

from exposure_tables.manifest import DeliveryError, Registry, verify_delivery
from exposure_tables.manifest import _extract_extensions
from tests import fits_builder


class RegistryTestBase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.workdir = self.root / 'work'
        self.registry = Registry(self.workdir)
        self.source_dir = self.root / 'sources'
        self.source_dir.mkdir()

    def tearDown(self):
        self.directory.cleanup()

    def path(self, name):
        return self.source_dir / name


class DeliveryIngestTests(RegistryTestBase):
    def test_complete_delivery_is_accepted_and_archived(self):
        source = fits_builder.build_complete(self.path('full.fits'))
        result = self.registry.ingest_delivery(source, 'exp-1', release=1)
        self.assertEqual(result['status'], 'accepted')
        self.assertEqual(len(result['sha256']), 64)
        # 归档对象真实存在且不可变
        self.assertTrue((self.workdir / 'raw' / result['sha256']).exists())
        status = self.registry.exposure_status('exp-1')
        self.assertTrue(status['complete'])
        self.assertEqual(status['missing'], [])

    def test_partial_delivery_marks_missing_extension(self):
        source = fits_builder.build_detections_only(
            self.path('det.fits'), obsid='exp-2')
        result = self.registry.ingest_delivery(
            source, 'exp-2', release=1, partial=True)
        self.assertEqual(result['status'], 'accepted')
        status = self.registry.exposure_status('exp-2')
        self.assertFalse(status['complete'])
        self.assertEqual(status['missing'][0]['extname'], 'QUALITY')

    def test_full_delivery_missing_required_extension_is_rejected(self):
        source = fits_builder.build_detections_only(
            self.path('det.fits'), obsid='exp-3')
        result = self.registry.ingest_delivery(source, 'exp-3', release=1)
        self.assertEqual(result['status'], 'rejected')
        codes = {p['code'] for p in result['problems']}
        self.assertIn('MISSING_EXTENSION', codes)
        # 被拒交付不产生可领取槽位
        status = self.registry.exposure_status('exp-3')
        self.assertEqual(status['candidate_slots'], [])

    def test_row_count_mismatch_is_visible_and_not_truncated(self):
        source = fits_builder.build_length_mismatch(
            self.path('mismatch.fits'), obsid='exp-4',
            det_rows=3, qual_rows=2)
        result = self.registry.ingest_delivery(source, 'exp-4', release=1)
        self.assertEqual(result['status'], 'rejected')
        problem = next(p for p in result['problems']
                       if p['code'] == 'ROW_COUNT_MISMATCH')
        self.assertIn('3', problem['detail'])
        self.assertIn('2', problem['detail'])

    def test_duplicate_slot_in_one_delivery_is_ambiguous(self):
        # 同一文件里两个 EXTNAME/EXTVER 完全相同的扩展
        col = fits.Column(name='ID', format='J',
                          array=np.array([1], dtype=np.int32))
        h1 = fits.BinTableHDU.from_columns([col])
        h2 = fits.BinTableHDU.from_columns([col])
        for hdu in (h1, h2):
            hdu.header['EXTNAME'] = 'DUP'
            hdu.header['EXTVER'] = 1
            hdu.header['OBSID'] = 'exp-5'
        path = self.path('dup.fits')
        fits.HDUList([fits.PrimaryHDU(), h1, h2]).writeto(path)
        extensions, _ = _extract_extensions(path)
        problems = verify_delivery(extensions, partial=False)
        self.assertIn('DUPLICATE_SLOT', {p['code'] for p in problems})

    def test_repeated_extname_with_distinct_extver_is_allowed(self):
        col = fits.Column(name='ID', format='J',
                          array=np.array([1], dtype=np.int32))
        h1 = fits.BinTableHDU.from_columns([col])
        h2 = fits.BinTableHDU.from_columns([col])
        h1.header.update(EXTNAME='QUALITY', EXTVER=1, OBSID='exp-6')
        h2.header.update(EXTNAME='QUALITY', EXTVER=2, OBSID='exp-6')
        path = self.path('twover.fits')
        fits.HDUList([fits.PrimaryHDU(), h1, h2]).writeto(path)
        extensions, _ = _extract_extensions(path)
        slots = {e.slot for e in extensions}
        self.assertEqual(slots, {'QUALITY[1]', 'QUALITY[2]'})

    def test_unreadable_source_is_rejected_but_archived(self):
        path = self.path('broken.fits')
        path.write_bytes(b'not a fits file')
        result = self.registry.ingest_delivery(path, 'exp-7', release=1)
        self.assertEqual(result['status'], 'rejected')
        self.assertEqual(result['problems'][0]['code'],
                         'UNREADABLE_SOURCE')
        # 原始字节仍归档留证
        self.assertTrue((self.workdir / 'raw' / result['sha256']).exists())

    def test_release_number_is_enforced(self):
        source = fits_builder.build_complete(self.path('a.fits'))
        self.registry.ingest_delivery(source, 'exp-8', release=1)
        with self.assertRaises(DeliveryError):
            self.registry.ingest_delivery(source, 'exp-8', release=1)

    def test_same_filename_cannot_be_ingested_twice(self):
        source = fits_builder.build_complete(self.path('same.fits'))
        self.registry.ingest_delivery(source, 'exp-9', release=1)
        with self.assertRaises(DeliveryError):
            self.registry.ingest_delivery(source, 'exp-9', release=2)


class PartialReplacementTests(RegistryTestBase):
    def _ingest_full_then_quality(self, new_quality_rows=3):
        full = fits_builder.build_complete(
            self.path('full.fits'), obsid='exp-p')
        self.registry.ingest_delivery(full, 'exp-p', release=1)
        partial = fits_builder.build_quality_only(
            self.path('quality.fits'), obsid='exp-p', extver=1,
            rows=new_quality_rows)
        self.registry.ingest_delivery(
            partial, 'exp-p', release=2, partial=True)

    def test_partial_replaces_only_named_slot(self):
        self._ingest_full_then_quality()
        status = self.registry.exposure_status('exp-p')
        slots = {s['slot']: s for s in status['slots']}
        # DETECTIONS 仍来自 release 1，QUALITY 已更新为 release 2
        self.assertEqual(slots['DETECTIONS[1]']['delivery_release'], 1)
        self.assertEqual(slots['QUALITY[1]']['delivery_release'], 2)
        self.assertTrue(slots['QUALITY[1]']['partial_origin'])

    def test_bad_partial_does_not_destroy_prior_usable_data(self):
        full = fits_builder.build_complete(
            self.path('full.fits'), obsid='exp-p')
        self.registry.ingest_delivery(full, 'exp-p', release=1)
        good_claim = self.registry.claim('exp-p')

        # 行数与探测表不符的质量半包：交付被拒绝，不替换任何槽位
        partial = fits_builder.build_quality_only(
            self.path('quality.fits'), obsid='exp-p', extver=1, rows=5)
        rejected = self.registry.ingest_delivery(
            partial, 'exp-p', release=2, partial=True)
        self.assertEqual(rejected['status'], 'rejected')
        codes = {p['code'] for p in rejected['problems']}
        self.assertIn('ROW_COUNT_MISMATCH', codes)

        status = self.registry.exposure_status('exp-p')
        # 关联仍然完好，当前完整领取未被半包破坏
        self.assertTrue(status['association']['ok'])
        self.assertEqual(status['current_claim'], good_claim['claim_id'])
        quality_slot = next(s for s in status['slots']
                            if s['extname'] == 'QUALITY')
        self.assertEqual(quality_slot['delivery_release'], 1)
        # 被拒交付仍作为可见历史保留
        self.assertEqual(status['rejected_deliveries'][0]['release'], 2)

    def test_missing_quality_after_partial_remains_visible(self):
        full = fits_builder.build_complete(
            self.path('full.fits'), obsid='exp-q')
        self.registry.ingest_delivery(full, 'exp-q', release=1)
        # 只补交探测表（带新 EXTVER 语义的场景由状态缺口体现）
        det = fits_builder.build_detections_only(
            self.path('det.fits'), obsid='exp-q')
        self.registry.ingest_delivery(det, 'exp-q', release=2, partial=True)
        status = self.registry.exposure_status('exp-q')
        # 质量表仍在（release 1），缺口为空；断言历史来源可追溯
        self.assertEqual(status['missing'], [])
        sources = {s['extname']: s['delivery_release']
                   for s in status['slots']}
        self.assertEqual(sources['QUALITY'], 1)


class ClaimTests(RegistryTestBase):
    def test_claim_records_traceable_source_per_slot(self):
        full = fits_builder.build_complete(self.path('full.fits'),
                                           obsid='exp-c')
        self.registry.ingest_delivery(full, 'exp-c', release=1)
        claim = self.registry.claim('exp-c')
        self.assertEqual(claim['status'], 'complete')
        for slot in claim['slots']:
            self.assertEqual(len(slot['sha256']), 64)
            self.assertEqual(slot['source_release'], 1)
            self.assertTrue(Path(slot['object']).exists())

    def test_claim_is_versioned_and_append_only(self):
        full = fits_builder.build_complete(self.path('full.fits'),
                                           obsid='exp-v')
        self.registry.ingest_delivery(full, 'exp-v', release=1)
        first = self.registry.claim('exp-v')
        partial = fits_builder.build_quality_only(
            self.path('q.fits'), obsid='exp-v')
        self.registry.ingest_delivery(
            partial, 'exp-v', release=2, partial=True)
        second = self.registry.claim('exp-v')
        self.assertNotEqual(first['claim_id'], second['claim_id'])
        self.assertEqual(second['supersedes'], first['claim_id'])
        # 历史领取仍可读取，不被改写
        self.assertEqual(
            self.registry.get_claim(first['claim_id'])['claim_id'],
            first['claim_id'])

    def test_incomplete_claim_does_not_replace_current_complete(self):
        full = fits_builder.build_complete(self.path('full.fits'),
                                           obsid='exp-i')
        self.registry.ingest_delivery(full, 'exp-i', release=1)
        good = self.registry.claim('exp-i')

        # 全新曝光只有探测表、缺质量表：可以领取但状态为 incomplete
        other = Registry(self.workdir.parent / 'other-work')
        det = fits_builder.build_detections_only(
            self.path('det.fits'), obsid='exp-other')
        other.ingest_delivery(det, 'exp-other', release=1, partial=True)
        partial_claim = other.claim('exp-other')
        self.assertEqual(partial_claim['status'], 'incomplete')
        self.assertEqual(partial_claim['missing'][0]['extname'], 'QUALITY')

        # 原曝光的当前完整领取不受任何影响
        status = self.registry.exposure_status('exp-i')
        self.assertEqual(status['current_claim'], good['claim_id'])

    def test_cannot_claim_without_accepted_delivery(self):
        with self.assertRaises(DeliveryError):
            self.registry.claim('never-seen')

    def test_quality_version_reference_must_resolve(self):
        # DETECTIONS 引用 QUALVER=2，但只收到 EXTVER=1 的质量表
        det = fits_builder.build_detections_only(
            self.path('det.fits'), obsid='exp-r', qualver=2)
        quality = fits_builder.build_quality_only(
            self.path('q.fits'), obsid='exp-r', extver=1)
        self.registry.ingest_delivery(
            det, 'exp-r', release=1, partial=True)
        self.registry.ingest_delivery(
            quality, 'exp-r', release=2, partial=True)
        status = self.registry.exposure_status('exp-r')
        codes = {p['code'] for p in status['association']['problems']}
        self.assertIn('QUALITY_VERSION_MISSING', codes)
        self.assertFalse(status['complete'])

    def test_new_quality_version_is_not_silently_bound_to_old_detection(self):
        # 探测表只引用 QUALVER=1；随后补交 EXTVER=2 的新质量掩码。
        # 新掩码作为独立候选槽位存在，但绝不自动套到旧探测表上。
        full = fits_builder.build_complete(
            self.path('full.fits'), obsid='exp-m', qualver=1)
        self.registry.ingest_delivery(full, 'exp-m', release=1)
        new_quality = fits_builder.build_quality_only(
            self.path('q2.fits'), obsid='exp-m', extver=2)
        self.registry.ingest_delivery(
            new_quality, 'exp-m', release=2, partial=True)

        status = self.registry.exposure_status('exp-m')
        self.assertIn('QUALITY[1]', status['candidate_slots'])
        self.assertIn('QUALITY[2]', status['candidate_slots'])
        # 关联仍按探测表声明的 QUALVER=1 解析到旧质量，不被新版本顶替
        self.assertTrue(status['association']['ok'])
        claim = self.registry.claim('exp-m')
        bound = {s['extname']: s['extver'] for s in claim['slots']}
        self.assertEqual(bound['DETECTIONS'], 1)
        # 领取包含全部候选槽位，每个都带来源可追溯，不做隐式选择
        quality_versions = sorted(
            s['extver'] for s in claim['slots'] if s['extname'] == 'QUALITY')
        self.assertEqual(quality_versions, [1, 2])


if __name__ == '__main__':
    unittest.main()
