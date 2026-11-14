import tempfile
import unittest
from pathlib import Path

import numpy as np
from astropy.io import fits

from exposure_tables.tables import TableCursor, parse_tform


def build_table(path, columns, name='DETECTIONS', extver=1, obsid='exposure-a',
                primary=True, extra_header=None):
    hdu = fits.BinTableHDU.from_columns(columns)
    hdu.name = name
    hdu.header['EXTVER'] = extver
    hdu.header['OBSID'] = obsid
    for key, value in (extra_header or {}).items():
        hdu.header[key] = value
    hdus = [fits.PrimaryHDU()] if primary else []
    if primary:
        hdus[0].header['OBSID'] = obsid
    hdus.append(hdu)
    fits.HDUList(hdus).writeto(path, overwrite=True)
    return path


class PhysicalValueTests(unittest.TestCase):
    def test_scaling_zero_null_and_units(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'scaled.fits'
            values = np.array([10, 20, 30], dtype=np.int16)
            column = fits.Column(name='V', format='I', unit='adu', array=values)
            hdu = fits.BinTableHDU.from_columns([column])
            hdu.name = 'DETECTIONS'
            hdu.header['EXTVER'] = 1
            hdu.header['OBSID'] = 'exposure-a'
            hdu.header['TSCAL1'] = 2.0
            hdu.header['TZERO1'] = 100.0
            hdu.header['TNULL1'] = 20
            fits.HDUList([fits.PrimaryHDU(), hdu]).writeto(path)
            with TableCursor(path, hdu_index=1) as cursor:
                rows = list(cursor.rows())
            self.assertEqual(rows[0]['values']['V']['raw'], 10)
            self.assertEqual(rows[0]['values']['V']['physical'], 120.0)
            self.assertEqual(rows[0]['values']['V']['unit'], 'adu')
            self.assertEqual(rows[0]['values']['V']['scale'], 2.0)
            self.assertEqual(rows[0]['values']['V']['zero'], 100.0)
            self.assertIsNone(rows[1]['values']['V']['physical'])
            self.assertEqual(rows[1]['values']['V']['invalid'], 'null')
            self.assertEqual(rows[2]['values']['V']['physical'], 160.0)

    def test_nan_is_not_zero_and_keeps_raw_bits(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'nan.fits'
            column = fits.Column(name='S', format='E',
                                 array=np.array([np.nan, 0.0], dtype=np.float32))
            build_table(path, [column])
            with TableCursor(path, identity={'obsid': 'exposure-a',
                                             'name': 'DETECTIONS',
                                             'extver': 1}) as cursor:
                rows = list(cursor.rows())
            self.assertIsNone(rows[0]['values']['S']['physical'])
            self.assertEqual(rows[0]['values']['S']['invalid'], 'nan')
            self.assertEqual(rows[0]['values']['S']['raw_bits'], '7fc00000')
            self.assertIsNone(rows[0]['values']['S']['raw'])
            self.assertEqual(rows[1]['values']['S']['physical'], 0.0)
            self.assertIsNone(rows[1]['values']['S']['invalid'])

    def test_bit_array_keeps_bit_order_and_raw_byte(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'bits.fits'
            column = fits.Column(name='MASK', format='8X',
                                 array=np.array([[0x81], [0x02]], dtype=np.uint8))
            build_table(path, [column], name='QUALITY')
            with TableCursor(path, hdu_index=1) as cursor:
                rows = list(cursor.rows())
            self.assertEqual(rows[0]['values']['MASK']['physical'],
                             [True, False, False, False, False, False, False, True])
            self.assertEqual(rows[0]['values']['MASK']['set_bits'], [0, 7])
            self.assertEqual(rows[0]['values']['MASK']['raw'], '81')
            self.assertEqual(rows[1]['values']['MASK']['set_bits'], [6])

    def test_variable_length_keeps_row_linkage_and_empty_is_not_zero(self):
        # fixture 的 CANDIDATES 是 PE(2)：第一行两个值，第二行空数组。
        path = Path(__file__).resolve().parent.parent / 'fixtures' / 'exposure.fits'
        with TableCursor(path, hdu_index=1) as cursor:
            rows = list(cursor.rows())
        first = rows[0]['candidates']['CANDIDATES']
        second = rows[1]['candidates']['CANDIDATES']
        self.assertEqual(first['physical'], [1.0, 2.0])
        self.assertEqual(first['row'], 0)
        self.assertEqual(second['physical'], [])
        self.assertEqual(second['row'], 1)
        self.assertEqual(second['count'], 0)
        self.assertEqual(second['invalid'], None)
        # 空候选必须和空数值区分：空数组是显式的空列表，不是 NaN
        self.assertEqual(second['physical'], [])
        self.assertNotEqual(rows[1]['values']['SIGNAL']['invalid'], None)

    def test_variable_length_read_is_bounded_to_heap(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'heap.fits'
            column = fits.Column(name='C', format='PJ()',
                                 array=np.array([np.array([1, 2], dtype=np.int32)],
                                                dtype=object))
            build_table(path, [column])
            from exposure_tables.tables import scan_file
            data_start = scan_file(path)['hdus'][1]['data_offset']
            raw = bytearray(path.read_bytes())
            # 把描述符里的元素数改得很大，必须报错而不是读出界
            raw[data_start:data_start + 4] = (10 ** 6).to_bytes(4, 'big')
            path.write_bytes(raw)
            with TableCursor(path, hdu_index=1) as cursor:
                rows = list(cursor.rows())
            self.assertEqual(rows[0]['values']['C']['invalid'], 'heap_out_of_range')
            self.assertIsNone(rows[0]['values']['C']['physical'])


class FormTests(unittest.TestCase):
    def test_tform_widths(self):
        self.assertEqual(parse_tform('PE(2)')['width'], 8)
        self.assertEqual(parse_tform('8X')['width'], 1)
        self.assertEqual(parse_tform('8X')['bits'], 8)
        self.assertEqual(parse_tform('16X')['width'], 2)
        self.assertEqual(parse_tform('QJ()')['width'], 16)
        with self.assertRaises(Exception):
            parse_tform('ZZ')


if __name__ == '__main__':
    unittest.main()
