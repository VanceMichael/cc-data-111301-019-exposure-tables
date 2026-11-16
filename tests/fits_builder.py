"""测试用 FITS 构造助手：在临时目录里生成带特定语义的业务资料。

仅用于测试，不接触 fixtures/ 之外的外部数据。
"""

from astropy.io import fits
import numpy as np


def _primary(obsid=None):
    hdu = fits.PrimaryHDU()
    if obsid is not None:
        hdu.header['OBSID'] = obsid
    return hdu


def build_complete(path, obsid='exp-1', qualver=1, rows=None,
                   signal_values=None, candidates=None, masks=None):
    """构造一份含 DETECTIONS（缩放/空值/变长候选）+ QUALITY（位数组）的整包。"""
    if rows is None:
        rows = 3
    if signal_values is None:
        signal_values = [1.0, float('nan'), 3.0]
    if candidates is None:
        candidates = [
            np.array([1.0, 2.0], dtype=np.float32),
            np.array([], dtype=np.float32),
            np.array([7.0], dtype=np.float32),
        ]
    if masks is None:
        masks = np.array([
            [True, False, False, False, False, False, False, False],
            [False, True, False, False, False, False, False, False],
            [False, False, True, False, False, False, False, False],
        ])
    id_col = fits.Column(name='ID', format='J',
                         array=np.arange(rows, dtype=np.int32))
    # TNULL 整数列：-99 表示无效，必须与真实 0 区分
    null_col = fits.Column(
        name='CODE', format='J', null=-99,
        array=np.ma.array([0, -99, 2][:rows],
                          mask=[False, True, False][:rows]))
    # 带缩放与零点的整数列：物理值与原始表示不同。astropy 7.1.1 在 numpy2
    # 下回写 bscale 整数列有类型转换缺陷，因此直接写原始存储值再补 TSCAL/TZERO。
    gain_col = fits.Column(name='GAIN', format='J',
                           array=np.array([0, 1, 2][:rows], dtype=np.int32))
    signal_col = fits.Column(
        name='SIGNAL', format='E', unit='adu',
        array=np.array(signal_values, dtype=np.float32))
    cand_col = fits.Column(name='CANDIDATES', format='PE(2)',
                           array=candidates)
    det = fits.BinTableHDU.from_columns(
        [id_col, null_col, gain_col, signal_col, cand_col])
    det.header['TSCAL3'] = 2
    det.header['TZERO3'] = 1
    det.header['EXTNAME'] = 'DETECTIONS'
    det.header['EXTVER'] = 1
    det.header['OBSID'] = obsid
    det.header['QUALVER'] = qualver

    mask_col = fits.Column(name='MASK', format='8X', array=np.array(masks))
    quality = fits.BinTableHDU.from_columns([mask_col])
    quality.header['EXTNAME'] = 'QUALITY'
    quality.header['EXTVER'] = qualver
    quality.header['OBSID'] = obsid

    fits.HDUList([_primary(obsid), det, quality]).writeto(
        path, overwrite=True)
    return path


def build_quality_only(path, obsid='exp-1', extver=1, rows=3, masks=None):
    """构造一次只含 QUALITY 的部分补交。"""
    if masks is None:
        masks = np.zeros((rows, 8), dtype=bool)
        # 每行点亮不同的位，保证位数组语义可被观察
        for i in range(rows):
            masks[i, i % 8] = True
    mask_col = fits.Column(name='MASK', format='8X', array=np.array(masks))
    quality = fits.BinTableHDU.from_columns([mask_col])
    quality.header['EXTNAME'] = 'QUALITY'
    quality.header['EXTVER'] = extver
    quality.header['OBSID'] = obsid
    fits.HDUList([_primary(), quality]).writeto(path, overwrite=True)
    return path


def build_detections_only(path, obsid='exp-1', qualver=1, rows=3):
    """构造一次只含 DETECTIONS、缺质量表的不完整交付。"""
    id_col = fits.Column(name='ID', format='J',
                         array=np.arange(rows, dtype=np.int32))
    det = fits.BinTableHDU.from_columns([id_col])
    det.header['EXTNAME'] = 'DETECTIONS'
    det.header['EXTVER'] = 1
    det.header['OBSID'] = obsid
    det.header['QUALVER'] = qualver
    fits.HDUList([_primary(obsid), det]).writeto(path, overwrite=True)
    return path


def build_length_mismatch(path, obsid='exp-2', det_rows=3, qual_rows=2):
    """构造 DETECTIONS 与 QUALITY 行数不一致的坏交付。"""
    path = str(path)
    build_complete(path, obsid=obsid, rows=det_rows)
    # 用另一份行数不同的 QUALITY 替换第二个 HDU
    with fits.open(path) as hdus:
        masks = np.zeros((qual_rows, 8), dtype=bool)
        mask_col = fits.Column(name='MASK', format='8X', array=masks)
        new_quality = fits.BinTableHDU.from_columns([mask_col])
        new_quality.header['EXTNAME'] = 'QUALITY'
        new_quality.header['EXTVER'] = 1
        new_quality.header['OBSID'] = obsid
        hdus[1].header['QUALVER'] = 1
        hdus[1].header['OBSID'] = obsid
        new_hdus = fits.HDUList([hdus[0], hdus[1], new_quality])
        new_hdus.writeto(path + '.tmp', overwrite=True)
    import os
    os.replace(path + '.tmp', path)
    return path
