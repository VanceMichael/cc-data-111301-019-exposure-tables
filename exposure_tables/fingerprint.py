"""扩展指纹与校验：流式读取，不把文件复制进内存。

接收依据由三部分组成：
- 数据区 SHA-256（本工具自己计算的内容身份）；
- 头部原文 SHA-256（可回溯到接收时的原始字节）；
- 语义指纹（身份、行数、列定义、缩放/零点/空值等会影响解释的头卡）。

另外复核来源声明的 DATASUM。声明存在但不一致时不接收。
"""

from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path

from .tables import as_int, text


BLOCK = 1 << 20


def stream_digest(path, offset, size, block=BLOCK):
    """流式计算 [offset, offset+size) 的 SHA-256 与 FITS 32 位和。"""
    sha = hashlib.sha256()
    total = 0
    padded = 0
    with Path(path).open('rb') as reader:
        reader.seek(offset)
        remaining = size
        while remaining > 0:
            chunk = reader.read(min(block, remaining))
            if not chunk:
                break
            sha.update(chunk)
            total += len(chunk)
            if len(chunk) % 4:
                body = chunk[:len(chunk) - len(chunk) % 4]
                tail = chunk[len(body):] + b'\x00' * (4 - len(chunk) % 4)
                s = sum(struct.unpack('>{}I'.format(len(body) // 4), body)) if body else 0
                s += int.from_bytes(tail, 'big')
            else:
                s = sum(struct.unpack('>{}I'.format(len(chunk) // 4), chunk))
            padded += s
            while padded >> 32:
                padded = (padded & 0xFFFFFFFF) + (padded >> 32)
            remaining -= len(chunk)
    return {'sha256': sha.hexdigest(), 'bytes': total,
            'datasum': padded, 'complete': total == size}


def data_size(layout):
    if not layout or layout.get('row_bytes') is None or layout.get('rows_declared') is None:
        return None
    return (layout['row_bytes'] * layout['rows_declared'] +
            (layout.get('pcount') or 0))


def header_bytes(path, record):
    """只读取头部区段，不读取数据区。"""
    header_offset = record.get('header_offset')
    data_offset = record.get('data_offset')
    if header_offset is None:
        header_offset = 0
    if data_offset is not None and data_offset > header_offset:
        with Path(path).open('rb') as reader:
            reader.seek(header_offset)
            return reader.read(data_offset - header_offset)
    header = record['header']
    return header.tostring().encode('ascii')


def semantic_fields(record):
    """只保留会影响身份与数值解释的头卡，忽略卡片顺序和校验卡。"""
    header = record['header']
    layout = record['layout'] or {}
    return {
        'xtension': text(header.get('XTENSION')).upper(),
        'extname': text(header.get('EXTNAME')) or None,
        'extver': as_int(header.get('EXTVER')),
        'obsid': text(header.get('OBSID')) or None,
        'qualver': as_int(header.get('QUALVER')),
        'naxis1': as_int(header.get('NAXIS1')),
        'naxis2': as_int(header.get('NAXIS2')),
        'pcount': as_int(header.get('PCOUNT')),
        'gcount': as_int(header.get('GCOUNT')),
        'columns': [
            {key: column.get(key) for key in
             ('name', 'format', 'unit', 'scale', 'zero', 'null', 'disp', 'dim',
              'bits', 'max_length', 'variable')}
            for column in layout.get('columns', [])
        ],
    }


def semantic_digest(record):
    payload = json.dumps(semantic_fields(record), sort_keys=True,
                         ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def fingerprint_extension(path, record):
    """为一个扩展计算接收依据；不会解码或驻留表数据。"""
    layout = record['layout']
    header = record['header']
    size = data_size(layout)
    offset = record.get('data_offset')
    digest = None
    if offset is not None and size is not None:
        digest = stream_digest(path, offset, size)
    raw_header = header_bytes(path, record)
    declared = as_int(header.get('DATASUM'))
    computed_datasum = digest['datasum'] if digest else None
    datasum_verified = None
    if declared is not None and computed_datasum is not None:
        datasum_verified = declared == computed_datasum
    return {
        'data_sha256': digest['sha256'] if digest else None,
        'data_bytes': digest['bytes'] if digest else None,
        'data_complete': digest['complete'] if digest else None,
        'computed_datasum': computed_datasum,
        'datasum_declared': declared,
        'datasum_verified': datasum_verified,
        'header_sha256': hashlib.sha256(raw_header).hexdigest(),
        'semantic_sha256': semantic_digest(record),
        'semantic': semantic_fields(record),
    }
