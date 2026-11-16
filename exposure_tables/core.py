"""FITS 容器的结构清点。

只读取表头，不载入数据区。HDU 的排列位置仅作回溯线索，扩展身份必须由
OBSID + EXTNAME + EXTVER 共同确定；同一 EXTNAME 允许重复出现。
"""

from pathlib import Path

from astropy.io import fits


def _column_summary(column):
    header = getattr(column, 'header', None)
    bscale = column.bscale if column.bscale is not None else 1
    bzero = column.bzero if column.bzero is not None else 0
    return {
        'name': column.name,
        'format': column.format,
        'unit': column.unit,
        'bscale': bscale,
        'bzero': bzero,
        'null': column.null,
        'dim': column.dim,
        # P/PE/Q/QE 为指向 heap 的变长列，rX 为位数组
        'variable_length': str(column.format).upper().startswith(('P', 'Q')),
        'bit_array': str(column.format).upper().endswith('X'),
    }


def inspect_file(path):
    """清点一份 FITS 来源的 HDU 与扩展身份信息（不读取数据区）。"""
    path = Path(path)
    hdus_view = []
    with fits.open(path, memmap=True, lazy_load_hdus=True) as hdus:
        for index, hdu in enumerate(hdus):
            info = hdu.fileinfo() or {}
            entry = {
                'index': index,
                'name': hdu.name,
                'extver': hdu.header.get('EXTVER'),
                'kind': type(hdu).__name__,
                'rows': hdu.header.get('NAXIS2'),
                'obsid': hdu.header.get('OBSID'),
                'qualver': hdu.header.get('QUALVER'),
                'header_location': info.get('hdrLoc'),
                'data_location': info.get('datLoc'),
                'data_span': info.get('datSpan'),
            }
            if isinstance(hdu, fits.BinTableHDU):
                entry['columns'] = [_column_summary(c) for c in hdu.columns]
                entry['heap_bytes'] = hdu.header.get('PCOUNT', 0)
            else:
                entry['columns'] = []
                entry['heap_bytes'] = 0
            hdus_view.append(entry)
    return {'source': path.name, 'hdus': hdus_view}
