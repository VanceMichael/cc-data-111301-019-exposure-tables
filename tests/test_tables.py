"""二进制表语义保留与按需读取测试。"""

from pathlib import Path
import tempfile
import unittest

from astropy.io import fits
import numpy as np

from exposure_tables.tables import open_table
from tests.fits_builder import build_complete


class TableSemanticsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'complete.fits'
        build_complete(self.path)

    def tearDown(self):
        self.directory.cleanup()

    def test_scaling_and_zero_distinguish_raw_and_physical(self):
        with open_table(self.path, 1) as table:
            gain = table.column('GAIN')
            self.assertEqual((gain.bscale, gain.bzero), (2.0, 1.0))
            rows = table.read_rows()
        # 原始存储 0/1/2，物理值 = 2*x+1 = 1/3/5
        self.assertEqual([r['GAIN']['raw'] for r in rows], [0, 1, 2])
        self.assertEqual([r['GAIN']['physical'] for r in rows], [1, 3, 5])

    def test_integer_null_is_not_filled_with_zero(self):
        with open_table(self.path, 1) as table:
            rows = table.read_rows()
        code = [r['CODE'] for r in rows]
        # 第 0 行是真实的 0，第 1 行是 TNULL=-99 无效值
        self.assertEqual(code[0]['raw'], 0)
        self.assertEqual(code[0]['physical'], 0)
        self.assertTrue(code[0]['valid'])
        self.assertEqual(code[1]['raw'], -99)
        self.assertIsNone(code[1]['physical'])
        self.assertFalse(code[1]['valid'])

    def test_float_nan_is_invalid_but_real_zero_stays_zero(self):
        with open_table(self.path, 1) as table:
            rows = table.read_rows()
        signal = [r['SIGNAL'] for r in rows]
        self.assertEqual(signal[0]['physical'], 1.0)
        self.assertTrue(signal[0]['valid'])
        # NaN：物理值为 null，明确标记无效，绝不补零
        self.assertIsNone(signal[1]['physical'])
        self.assertFalse(signal[1]['valid'])
        self.assertEqual(signal[1]['raw'], {'nan': True})
        self.assertEqual(signal[2]['physical'], 3.0)

    def test_unit_is_reported(self):
        with open_table(self.path, 1) as table:
            signal = table.column('SIGNAL')
            self.assertEqual(signal.unit, 'adu')

    def test_bit_array_preserves_bit_order_and_raw_bytes(self):
        with open_table(self.path, 2) as table:
            row0 = table.read_row(0)
        mask = row0['MASK']
        # 第 0 位为真，对应原始字节 0x80（MSB-first）
        self.assertEqual(mask['raw'], [0x80])
        self.assertEqual(mask['physical'][0], True)
        self.assertFalse(any(mask['physical'][1:]))
        self.assertEqual(mask['bit_order'], 'MSB-first')

    def test_variable_length_candidates_trace_heap(self):
        with open_table(self.path, 1) as table:
            rows = table.read_rows()
        candidates = [r['CANDIDATES'] for r in rows]
        # 行 0：两个候选，描述符指向 heap 偏移 0
        self.assertEqual(candidates[0]['physical'], [1, 2])
        self.assertEqual(candidates[0]['descriptor'],
                         {'element_count': 2, 'heap_offset': 0})
        # 行 1：空候选数组，与 NaN 标量是不同含义
        self.assertEqual(candidates[1]['physical'], [])
        self.assertEqual(candidates[1]['descriptor']['element_count'], 0)
        # 行 2：单候选，复用 heap 偏移
        self.assertEqual(candidates[2]['physical'], [7])
        self.assertEqual(candidates[2]['descriptor']['heap_offset'], 8)

    def test_every_cell_traces_back_to_hdu_row_and_file(self):
        with open_table(self.path, 1) as table:
            cell = table.read_row(0)['ID']
        provenance = cell['provenance']
        self.assertEqual(provenance['hdu_index'], 1)
        self.assertEqual(provenance['extname'], 'DETECTIONS')
        self.assertEqual(provenance['row'], 0)
        self.assertEqual(provenance['column'], 'ID')
        self.assertEqual(Path(provenance['file']), self.path)

    def test_row_windowing_does_not_read_whole_table(self):
        with open_table(self.path, 1) as table:
            requested = []
            original = table._read

            def spy(offset, size):
                requested.append(size)
                return original(offset, size)

            table._read = spy
            table.read_row(0)
            # 单行读取只请求该行宽度的字节，而非整张表
            self.assertIn(table.row_width, requested)
            self.assertNotIn(table.row_width * table.nrows, requested)

    def test_metadata_available_without_loading_data(self):
        with open_table(self.path, 1) as table:
            metadata = table.metadata()
        self.assertEqual(metadata['rows'], 3)
        # 变长候选列存在 heap，字节数与表头 PCOUNT 一致且为正
        self.assertGreater(metadata['heap_bytes'], 0)
        self.assertEqual([c['name'] for c in metadata['columns']],
                         ['ID', 'CODE', 'GAIN', 'SIGNAL', 'CANDIDATES'])


class BitPaddingTests(unittest.TestCase):
    def test_non_byte_aligned_bit_column_drops_padding(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / 'bits.fits'
        # 3 个有意义位，占 1 字节，剩余 5 位是填充
        col = fits.Column(name='B', format='3X',
                          array=np.array([[True, False, True]]))
        hdu = fits.BinTableHDU.from_columns([col])
        hdu.header['EXTNAME'] = 'BITS'
        fits.HDUList([fits.PrimaryHDU(), hdu]).writeto(path, overwrite=True)
        with open_table(path, 1) as table:
            cell = table.read_row(0)['B']
        self.assertEqual(len(cell['physical']), 3)
        self.assertEqual(cell['physical'], [True, False, True])


if __name__ == '__main__':
    unittest.main()
