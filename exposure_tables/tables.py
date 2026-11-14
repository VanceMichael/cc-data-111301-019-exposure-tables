"""FITS 扩展头解析与按需按行读取。

清单阶段只读取头部；这里从不访问 ``HDUList.data``，因为对二进制表来说，
astropy 一旦访问 ``.data`` 就会把整张表读入内存。取数时用普通文件句柄
``seek`` 到目标行，变长列再按描述符读取对应的 heap 片段，内存占用只与
当前行和当前数组有关。

扩展身份由 (OBSID, EXTNAME, EXTVER) 组成，HDU 顺序只作为定位线索，并在
读取前用身份复核。缺失、重复或不能解析的扩展会作为问题返回，而不是用
位置顶替。
"""

from __future__ import annotations

import math
import re
import struct
from pathlib import Path

from astropy.io import fits


REQUIRED_TABLES = ('DETECTIONS', 'QUALITY')

_CODE_SIZE = {'L': 1, 'B': 1, 'I': 2, 'J': 4, 'K': 8,
              'A': 1, 'E': 4, 'D': 8, 'C': 8, 'M': 16, 'X': 1}

_STRUCT_CODE = {'B': 'B', 'I': 'h', 'J': 'i', 'K': 'q',
                'E': 'f', 'D': 'd', 'C': 'ff', 'M': 'dd'}

_INT_CODES = frozenset('BIJK')
_FLOAT_CODES = frozenset('ED')

_TFORM = re.compile(r'^(?P<repeat>[0-9]*)(?P<code>[LXBIJKAEDCMPQ])(?P<rest>.*)$')
_PFORM = re.compile(r'^(?P<code>[LXBIJKAEDCMPQ])(?:\((?P<max>[^)]*)\))?$')


class FitsTableError(ValueError):
    """来源不是可用的 FITS 二进制表。"""


class TableLayoutError(FitsTableError):
    """TFORM/NAXIS 等表布局自相矛盾，不能安全读取。"""


def issue(severity, code, message, **details):
    return {'severity': severity, 'code': code, 'message': message, 'details': details}


def as_int(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def as_float(value, default):
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def text(value):
    return value.strip() if isinstance(value, str) else ''


def json_float(value, width):
    """返回 (可 JSON 的数值, 无效原因, 原始位十六进制)。

    非有限浮点不能静默补成 0：物理值留空并附上原始位。
    """
    if math.isfinite(value):
        return value, None, None
    reason = 'nan' if math.isnan(value) else ('infinity' if value > 0 else '-infinity')
    fmt = '>f' if width == 32 else '>d'
    try:
        bits = struct.pack(fmt, value).hex()
    except (struct.error, OverflowError):
        bits = None
    return None, reason, bits


def parse_tform(tform):
    """把 TFORMn 拆成可读取的布局。"""
    if not isinstance(tform, str) or not tform.strip():
        raise TableLayoutError('缺少 TFORM')
    match = _TFORM.match(tform.strip().upper())
    if not match:
        raise TableLayoutError('无法识别的 TFORM: {!r}'.format(tform))
    repeat = as_int(match.group('repeat')) or 1
    code = match.group('code')
    rest = match.group('rest').strip()
    result = {'format': tform.strip(), 'repeat': repeat, 'code': code,
              'element_code': code, 'max_length': None}
    if code in ('P', 'Q'):
        inner = _PFORM.match(rest)
        if not inner:
            raise TableLayoutError('无法识别的变长列格式: {!r}'.format(tform))
        result['element_code'] = inner.group('code')
        result['max_length'] = as_int(inner.group('max'))
        result['width'] = repeat * (8 if code == 'P' else 16)
    elif code == 'X':
        result['width'] = (repeat + 7) // 8
        result['bits'] = repeat
    elif code == 'A':
        result['width'] = repeat
    else:
        result['width'] = repeat * _CODE_SIZE[code]
    if rest and code not in ('P', 'Q'):
        raise TableLayoutError('TFORM 含无法解释的尾部: {!r}'.format(tform))
    return result


def parse_table_layout(header, file_size=None, data_offset=None):
    """解析一个 BINTABLE 的列布局，并检查行宽与数据区边界。"""
    layout = {'supported': False, 'readable': False, 'row_bytes': None,
              'rows_declared': None, 'pcount': None, 'gcount': None,
              'columns': [], 'issues': []}
    issues = layout['issues']
    if text(header.get('XTENSION')).upper() != 'BINTABLE':
        issues.append(issue('error', 'unsupported_table_type',
                            '只支持 FITS 二进制表（BINTABLE）'))
        return layout
    naxis1 = as_int(header.get('NAXIS1'))
    naxis2 = as_int(header.get('NAXIS2'))
    tfields = as_int(header.get('TFIELDS'))
    pcount = as_int(header.get('PCOUNT')) or 0
    gcount = as_int(header.get('GCOUNT')) or 1
    layout.update({'supported': True, 'row_bytes': naxis1,
                   'rows_declared': naxis2, 'pcount': pcount, 'gcount': gcount})
    if naxis1 is None or naxis2 is None or naxis1 < 0 or naxis2 < 0:
        issues.append(issue('error', 'invalid_table_size', 'NAXIS1/NAXIS2 缺失或非法'))
        return layout
    if gcount != 1:
        issues.append(issue('error', 'unsupported_gcount', '二进制表 GCOUNT 必须为 1'))
        return layout
    if tfields is None or tfields < 0:
        issues.append(issue('error', 'missing_tfields', '缺少 TFIELDS'))
        return layout
    offset = 0
    columns = []
    for index in range(1, tfields + 1):
        name = text(header.get('TTYPE{}'.format(index)))
        tform = header.get('TFORM{}'.format(index))
        try:
            parsed = parse_tform(tform)
        except TableLayoutError as error:
            issues.append(issue('error', 'invalid_tform', str(error),
                                column=name or 'TTYPE{}'.format(index)))
            return layout
        if parsed['code'] in ('P', 'Q') and parsed['element_code'] in ('P', 'Q'):
            issues.append(issue('error', 'nested_variable_column',
                                '变长列不能嵌套变长元素', column=name))
            return layout
        column = {
            'name': name or 'COLUMN{}'.format(index), 'index': index,
            'format': parsed['format'], 'repeat': parsed['repeat'],
            'code': parsed['code'], 'element_code': parsed['element_code'],
            'max_length': parsed['max_length'], 'width': parsed['width'],
            'offset': offset, 'variable': parsed['code'] in ('P', 'Q'),
            'bits': parsed.get('bits'),
            'unit': header.get('TUNIT{}'.format(index)),
            'scale': as_float(header.get('TSCAL{}'.format(index)), 1.0),
            'zero': as_float(header.get('TZERO{}'.format(index)), 0.0),
            'null': as_int(header.get('TNULL{}'.format(index))),
            'disp': header.get('TDISP{}'.format(index)),
            'dim': header.get('TDIM{}'.format(index)),
        }
        if not name:
            issues.append(issue('error', 'unnamed_column',
                                '第 {} 列缺少 TTYPE'.format(index),
                                column=column['name']))
        offset += parsed['width']
        columns.append(column)
    layout['columns'] = columns
    if offset != naxis1:
        issues.append(issue('error', 'row_width_mismatch',
                            'TFORM 宽度合计 {} 与 NAXIS1 {} 不符'.format(offset, naxis1),
                            expected=offset, actual=naxis1))
        return layout
    data_bytes = naxis1 * naxis2 + pcount
    if file_size is not None and data_offset is not None:
        if data_offset < 0 or data_offset + data_bytes > file_size:
            issues.append(issue('error', 'truncated_data', '声明的行数超出文件数据区',
                                declared_rows=naxis2, data_bytes=data_bytes,
                                available=max(0, file_size - data_offset)))
            return layout
    if any(column['variable'] for column in columns) and pcount <= 0:
        issues.append(issue('error', 'missing_heap', '存在变长列但 PCOUNT 为 0'))
        return layout
    layout['readable'] = not any(item['severity'] == 'error' for item in issues)
    return layout


def column_summary(column):
    """清单中公开的列信息；不读取数据。"""
    return {
        'name': column['name'], 'format': column['format'],
        'unit': column['unit'], 'repeat': column['repeat'],
        'code': column['code'], 'element_code': column['element_code'],
        'variable': column['variable'], 'max_length': column['max_length'],
        'bits': column['bits'], 'scale': column['scale'], 'zero': column['zero'],
        'null': column['null'], 'disp': column['disp'], 'dim': column['dim'],
        'width': column['width'], 'offset': column['offset'],
    }


def scan_file(path):
    """只读取头部，返回 HDU 元数据；不加载任何表数据。"""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    file_size = path.stat().st_size
    records = []
    with fits.open(path, memmap=True, lazy_load_hdus=True) as hdus:
        for index, hdu in enumerate(hdus):
            header = hdu.header.copy()
            info = hdu.fileinfo()
            kind = type(hdu).__name__
            data_offset = info.get('datLoc')
            layout = None
            if kind == 'BinTableHDU' or text(header.get('XTENSION')).upper() == 'BINTABLE':
                layout = parse_table_layout(header, file_size=file_size,
                                            data_offset=data_offset)
            records.append({'index': index, 'kind': kind, 'header': header,
                            'header_offset': info.get('hdrLoc'),
                            'data_offset': data_offset,
                            'data_span': info.get('datSpan'),
                            'layout': layout})
    return {'path': path, 'size': file_size, 'hdus': records}


def extension_identity(header, primary_obsid=None):
    """从头部取扩展身份；EXTVER 缺失时按 FITS 语义视为 1，但会标记。"""
    name = text(header.get('EXTNAME'))
    declared = 'EXTVER' in header
    extver = as_int(header.get('EXTVER')) if declared else (1 if name else None)
    own_obsid = text(header.get('OBSID'))
    obsid = own_obsid or primary_obsid
    identity = {
        'obsid': obsid or None,
        'name': name or None,
        'extver': extver,
        'extver_declared': declared,
        'obsid_source': 'extension' if own_obsid else ('primary' if primary_obsid else None),
    }
    identity['valid'] = bool(identity['obsid'] and identity['name'] and extver is not None)
    identity['key'] = extension_key(identity) if identity['valid'] else None
    return identity


def extension_key(identity):
    if identity and identity.get('valid'):
        return '{}|{}|{}'.format(identity['obsid'], identity['name'], identity['extver'])
    return None


def _raw_value(code, value):
    """返回可安全序列化的原始表示；非有限浮点留给 raw_bits。"""
    if code in _FLOAT_CODES and not math.isfinite(value):
        return None
    return value


def _element_record(code, value, column):
    """解释一个数值元素，返回 (physical, invalid_reason, raw_bits)。"""
    if code in _INT_CODES and column['null'] is not None and value == column['null']:
        return None, 'null', None
    if code in _FLOAT_CODES and not math.isfinite(value):
        physical, reason, bits = json_float(value, 32 if code == 'E' else 64)
        return physical, reason, bits
    scaled = column['scale'] != 1.0 or column['zero'] != 0.0
    physical = value * column['scale'] + column['zero'] if scaled else value
    if isinstance(physical, float) and not math.isfinite(physical):
        _, reason, bits = json_float(physical, 64)
        return None, reason or 'non_finite', bits
    return physical, None, None


def _scalar_or_list(values, repeat):
    if repeat == 1 and len(values) == 1:
        return values[0]
    return values


def _decode_numeric(column, segment):
    code = column['code']
    repeat = column['repeat']
    fmt = '>' + _STRUCT_CODE[code] * repeat
    size = struct.calcsize(fmt)
    if len(segment) < size:
        return {'raw': None, 'physical': None, 'invalid': 'short_column',
                'raw_bits': None}
    raw_values = list(struct.unpack_from(fmt, segment))
    if code in ('C', 'M'):
        width = 32 if code == 'C' else 64
        pairs = [(raw_values[i], raw_values[i + 1])
                 for i in range(0, len(raw_values), 2)]
        physical = []
        invalids = []
        raw_bits = []
        for pair in pairs:
            components = []
            reasons = []
            bits = []
            for value in pair:
                decoded, reason, raw_bit = _element_record(
                    'E' if width == 32 else 'D', value, column)
                components.append(decoded)
                reasons.append(reason)
                bits.append(raw_bit)
            physical.append(components)
            invalids.append(reasons if any(reasons) else None)
            raw_bits.append(bits if any(bits) else None)
        pair_code = 'E' if width == 32 else 'D'
        raw_pairs = [[_raw_value(pair_code, left), _raw_value(pair_code, right)]
                     for left, right in pairs]
        return {'raw': _scalar_or_list(raw_pairs, repeat),
                'physical': _scalar_or_list(physical, repeat),
                'invalid': _scalar_or_list(invalids, repeat),
                'raw_bits': _scalar_or_list(raw_bits, repeat)}
    if repeat > 1:
        physical = []
        invalids = []
        raw_bits = []
        for value in raw_values:
            decoded, reason, raw_bit = _element_record(code, value, column)
            physical.append(decoded)
            invalids.append(reason)
            raw_bits.append(raw_bit)
        return {'raw': [_raw_value(code, value) for value in raw_values],
                'physical': physical,
                'invalid': invalids if any(invalids) else None,
                'raw_bits': raw_bits if any(raw_bits) else None}
    decoded, reason, raw_bit = _element_record(code, raw_values[0], column)
    return {'raw': _raw_value(code, raw_values[0]), 'physical': decoded,
            'invalid': reason, 'raw_bits': raw_bit}


def _decode_logical(segment, repeat):
    chars = segment[:repeat].decode('latin-1')
    if not chars:
        return {'raw': '', 'physical': None, 'invalid': 'empty_column'}
    if any(char not in 'TF' for char in chars):
        return {'raw': chars, 'physical': None, 'invalid': 'invalid_logical'}
    physical = [char == 'T' for char in chars]
    return {'raw': chars, 'physical': physical[0] if repeat == 1 else physical,
            'invalid': None}


def _decode_string(segment):
    raw_hex = segment.hex()
    try:
        physical = segment.decode('ascii')
    except UnicodeDecodeError:
        return {'raw': raw_hex, 'physical': None,
                'invalid': 'non_ascii_string', 'raw_hex': raw_hex}
    return {'raw': raw_hex, 'physical': physical.rstrip(' \x00'), 'invalid': None}


def _decode_bits(bit_count, segment):
    bits = []
    for index in range(bit_count):
        byte_index = index // 8
        if byte_index >= len(segment):
            return {'raw': segment.hex(), 'physical': None,
                    'invalid': 'short_bit_array', 'raw_hex': segment.hex()}
        bits.append(bool(segment[byte_index] & (0x80 >> (index % 8))))
    return {'raw': segment.hex(), 'physical': bits, 'invalid': None,
            'raw_hex': segment.hex(),
            'set_bits': [index for index, value in enumerate(bits) if value],
            'bit_order': 'msb_first'}


def _decode_variable(column, descriptor, reader, heap_start, heap_bytes=None):
    fmt = '>ii' if column['code'] == 'P' else '>qq'
    descriptor_size = 8 if column['code'] == 'P' else 16
    if len(descriptor) < descriptor_size:
        return {'physical': None, 'raw': None, 'invalid': 'short_descriptor',
                'count': None, 'heap': None}
    count, offset = struct.unpack_from(fmt, descriptor)
    descriptor_hex = descriptor[:descriptor_size].hex()
    result = {'descriptor': {'count': count, 'offset': offset,
                             'format': column['format'], 'raw_hex': descriptor_hex},
              'max_length': column['max_length'], 'count': count}
    if count < 0 or offset < 0:
        result.update({'physical': None, 'raw': None,
                       'invalid': 'invalid_descriptor'})
        return result
    exceeded = column['max_length'] is not None and count > column['max_length']
    code = column['element_code']
    element_size = 1 if code in ('A', 'L', 'X') else _CODE_SIZE.get(code, 0)
    byte_count = (count + 7) // 8 if code == 'X' else count * element_size
    if heap_bytes is not None and offset + byte_count > heap_bytes:
        result.update({'physical': None, 'raw': None, 'max_length_exceeded': exceeded,
                       'invalid': 'heap_out_of_range'})
        return result
    reader.seek(heap_start + offset)
    payload = reader.read(byte_count)
    result['heap'] = {'offset': offset, 'bytes': byte_count,
                      'start': heap_start + offset,
                      'complete': len(payload) == byte_count}
    result['max_length_exceeded'] = exceeded
    if len(payload) != byte_count:
        result.update({'physical': None, 'raw': None,
                       'invalid': 'heap_out_of_range'})
        return result
    if not payload:
        result.update({'physical': [], 'raw': [], 'invalid': None,
                       'kind': 'empty_array'})
        return result
    if code == 'A':
        decoded = _decode_string(payload)
        result.update({'physical': decoded['physical'], 'raw': payload.hex(),
                       'invalid': decoded['invalid']})
        return result
    if code == 'L':
        decoded = _decode_logical(payload, count)
        result.update({'physical': decoded['physical'], 'raw': decoded['raw'],
                       'invalid': decoded['invalid']})
        return result
    if code == 'X':
        decoded = _decode_bits(count, payload)
        result.update({'physical': decoded['physical'], 'raw': payload.hex(),
                       'invalid': decoded['invalid'], 'raw_hex': payload.hex(),
                       'set_bits': decoded.get('set_bits'),
                       'bit_order': 'msb_first'})
        return result
    fmt = '>' + _STRUCT_CODE[code] * count
    size = struct.calcsize(fmt)
    if len(payload) < size:
        result.update({'physical': None, 'raw': None,
                       'invalid': 'short_variable_array'})
        return result
    raw_values = list(struct.unpack_from(fmt, payload))
    if code in ('C', 'M'):
        width = 32 if code == 'C' else 64
        pairs = [(raw_values[i], raw_values[i + 1])
                 for i in range(0, len(raw_values), 2)]
        physical = []
        invalids = []
        raw_bits = []
        for pair in pairs:
            components = []
            reasons = []
            bits = []
            for value in pair:
                decoded, reason, raw_bit = _element_record(
                    'E' if width == 32 else 'D', value, column)
                components.append(decoded)
                reasons.append(reason)
                bits.append(raw_bit)
            physical.append(components)
            invalids.append(reasons if any(reasons) else None)
            raw_bits.append(bits if any(bits) else None)
        pair_code = 'E' if width == 32 else 'D'
        raw_pairs = [[_raw_value(pair_code, left), _raw_value(pair_code, right)]
                     for left, right in pairs]
        result.update({'physical': physical, 'raw': raw_pairs,
                       'invalid': invalids if any(invalids) else None,
                       'raw_bits': raw_bits if any(raw_bits) else None})
        return result
    physical = []
    invalids = []
    raw_bits = []
    for value in raw_values:
        decoded, reason, raw_bit = _element_record(
            code, value, column)
        physical.append(decoded)
        invalids.append(reason)
        raw_bits.append(raw_bit)
    result.update({'physical': physical,
                   'raw': [_raw_value(code, value) for value in raw_values],
                   'invalid': invalids if any(invalids) else None,
                   'raw_bits': raw_bits if any(raw_bits) else None})
    return result


def decode_row(layout, row_bytes, reader, heap_start, heap_bytes=None):
    """把一行的原始字节解释成保留物理/原始含义的列记录。"""
    values = {}
    candidates = {}
    issues = []
    for column in layout['columns']:
        segment = row_bytes[column['offset']:column['offset'] + column['width']]
        if len(segment) != column['width']:
            decoded = {'physical': None, 'raw': None,
                       'invalid': 'short_row', 'unit': column['unit']}
        elif column['variable']:
            decoded = _decode_variable(column, segment, reader, heap_start,
                                       heap_bytes=heap_bytes)
        elif column['code'] == 'X':
            decoded = _decode_bits(column['bits'] or column['repeat'], segment)
        elif column['code'] == 'A':
            decoded = _decode_string(segment)
        elif column['code'] == 'L':
            decoded = _decode_logical(segment, column['repeat'])
        else:
            decoded = _decode_numeric(column, segment)
        decoded['unit'] = column['unit']
        decoded['format'] = column['format']
        decoded['scale'] = column['scale']
        decoded['zero'] = column['zero']
        decoded['null'] = column['null']
        decoded['code'] = column['code']
        decoded['variable'] = column['variable']
        values[column['name']] = decoded
        if column['variable']:
            candidates[column['name']] = {
                'count': decoded.get('count'),
                'physical': decoded.get('physical'),
                'raw': decoded.get('raw'),
                'invalid': decoded.get('invalid'),
                'descriptor': decoded.get('descriptor'),
                'max_length': column['max_length'],
                'max_length_exceeded': decoded.get('max_length_exceeded'),
                'heap': decoded.get('heap'),
                'unit': column['unit'], 'format': column['format'],
            }
        if decoded.get('invalid'):
            issues.append({'column': column['name'],
                           'code': decoded['invalid']})
    return values, candidates, issues


def _hdu_identity_record(header, primary_obsid):
    identity = extension_identity(header, primary_obsid)
    return {'identity': identity, 'datasum': header.get('DATASUM'),
            'checksum': header.get('CHECKSUM'),
            'extname': header.get('EXTNAME'), 'extver': header.get('EXTVER'),
            'obsid': header.get('OBSID'), 'qualver': header.get('QUALVER')}


def _primary_obsid(scan):
    if not scan['hdus']:
        return None
    return text(scan['hdus'][0]['header'].get('OBSID')) or None


def find_extension(scan, identity=None, hdu_index=None):
    """按身份定位扩展；位置只在身份复核后作为提示。"""
    primary = _primary_obsid(scan)
    matches = []
    for record in scan['hdus']:
        if record['layout'] is None:
            continue
        current = extension_identity(record['header'], primary)
        if identity is not None:
            if (current['obsid'] == identity.get('obsid') and
                    current['name'] == identity.get('name') and
                    current['extver'] == identity.get('extver')):
                matches.append(record)
        elif hdu_index is not None and record['index'] == hdu_index:
            matches.append(record)
    if identity is not None:
        if not matches:
            raise FitsTableError('找不到身份为 {} 的扩展'.format(
                identity.get('key') or identity))
        if len(matches) > 1:
            raise FitsTableError('身份重复，无法确定扩展: {}'.format(
                identity.get('key') or identity))
        record = matches[0]
        if hdu_index is not None and record['index'] != hdu_index:
            raise FitsTableError('扩展身份与记录的 HDU 位置不一致')
        return record
    if hdu_index is None:
        raise FitsTableError('需要扩展身份或 HDU 位置')
    if not matches:
        raise FitsTableError('没有可读的二进制扩展位于 HDU {}'.format(hdu_index))
    return matches[0]


class TableCursor:
    """一个扩展的按需行游标；文件句柄只在迭代期间打开。"""

    def __init__(self, path, identity=None, hdu_index=None, start=0, limit=None,
                 source=None):
        self.scan = scan_file(path)
        self.record = find_extension(self.scan, identity, hdu_index)
        self.layout = self.record['layout']
        if self.layout is None or not self.layout['readable']:
            codes = [item['code'] for item in (self.layout or {}).get('issues', [])]
            raise TableLayoutError('扩展布局不可读: {}'.format(codes))
        self.total_rows = self.layout['rows_declared']
        self.path = Path(path)
        self.start = int(start)
        if self.start < 0 or self.start > self.total_rows:
            raise ValueError('起始行超出范围: {}'.format(start))
        if limit is not None:
            if int(limit) < 0:
                raise ValueError('行数不能为负')
            self.limit = min(self.total_rows, self.start + int(limit))
        else:
            self.limit = self.total_rows
        self.row_bytes = self.layout['row_bytes']
        self.data_offset = self.record['data_offset']
        if self.data_offset is None:
            raise FitsTableError('缺少数据区偏移')
        self.heap_start = self.data_offset + self.row_bytes * self.total_rows
        self.heap_bytes = self.layout['pcount']
        primary = _primary_obsid(self.scan)
        info = _hdu_identity_record(self.record['header'], primary)
        self.extension = {
            'index': self.record['index'],
            'identity': {key: info['identity'][key] for key in
                         ('obsid', 'name', 'extver', 'extver_declared',
                          'obsid_source', 'valid')},
            'key': info['identity'].get('key'),
            'datasum': info['datasum'], 'checksum': info['checksum'],
            'quality_version': info['qualver'],
        }
        self.columns = [column_summary(column) for column in self.layout['columns']]
        self.source = {'path': str(self.path.resolve()), 'name': self.path.name,
                       'bytes': self.scan['size']}
        if source:
            self.source.update(source)
        self._reader = None

    def __enter__(self):
        self._reader = self.path.open('rb')
        return self

    def __exit__(self, *exc):
        if self._reader is not None:
            self._reader.close()
            self._reader = None

    def rows(self):
        if self._reader is not None:
            yield from self._iter(self._reader)
            return
        with self.path.open('rb') as reader:
            yield from self._iter(reader)

    def _iter(self, reader):
        for index in range(self.start, self.limit):
            reader.seek(self.data_offset + index * self.row_bytes)
            raw = reader.read(self.row_bytes)
            if len(raw) != self.row_bytes:
                raise FitsTableError(
                    '第 {} 行不完整: 需要 {} 字节，实际 {}'.format(
                        index, self.row_bytes, len(raw)))
            values, candidates, row_issues = decode_row(
                self.layout, raw, reader, self.heap_start,
                heap_bytes=self.heap_bytes)
            for candidate in candidates.values():
                candidate['row'] = index
            yield {'row': index, 'values': values,
                   'candidates': candidates, 'issues': row_issues,
                   'source': dict(self.source),
                   'extension': dict(self.extension),
                   'table': self.extension['identity']['name']}

    def at(self, index):
        if index < 0 or index >= self.total_rows:
            raise ValueError('行号超出范围: {}'.format(index))
        with self.path.open('rb') as reader:
            reader.seek(self.data_offset + index * self.row_bytes)
            raw = reader.read(self.row_bytes)
            if len(raw) != self.row_bytes:
                raise FitsTableError('第 {} 行不完整'.format(index))
            values, candidates, row_issues = decode_row(
                self.layout, raw, reader, self.heap_start,
                heap_bytes=self.heap_bytes)
            for candidate in candidates.values():
                candidate['row'] = index
            return {'row': index, 'values': values, 'candidates': candidates,
                    'issues': row_issues, 'source': dict(self.source),
                    'extension': dict(self.extension),
                    'table': self.extension['identity']['name']}

    def metadata(self):
        return {'source': dict(self.source), 'extension': dict(self.extension),
                'layout': {'row_bytes': self.row_bytes, 'rows': self.total_rows,
                           'pcount': self.layout['pcount'],
                           'gcount': self.layout['gcount']},
                'columns': list(self.columns)}


def read_rows(path, identity=None, hdu_index=None, start=0, limit=None,
              source=None):
    """读取有界行数并返回列表；大表请用 ``TableCursor``。"""
    with TableCursor(path, identity=identity, hdu_index=hdu_index,
                     start=start, limit=limit, source=source) as cursor:
        rows = list(cursor.rows())
        result = cursor.metadata()
    result.update({'rows_read': len(rows), 'start': start, 'rows': rows})
    return result


def iter_rows(path, identity=None, hdu_index=None, start=0, limit=None,
              source=None):
    """逐行产出记录，不在内存中保留整张表。"""
    with TableCursor(path, identity=identity, hdu_index=hdu_index,
                     start=start, limit=limit, source=source) as cursor:
        yield from cursor.rows()
