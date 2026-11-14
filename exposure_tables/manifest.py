"""曝光资料清单：只读头部，按扩展身份而不是 HDU 位置描述一次曝光。"""

from __future__ import annotations

from pathlib import Path

from .tables import (column_summary, extension_identity, issue, scan_file,
                     text)


def _manifest_extension(record, primary_obsid):
    header = record['header']
    layout = record['layout']
    identity = extension_identity(header, primary_obsid)
    entry = {
        'hdu': record['index'],
        'kind': record['kind'],
        'identity': identity,
        'key': identity.get('key'),
        'extname': text(header.get('EXTNAME')) or None,
        'extver': identity.get('extver'),
        'quality_version': header.get('QUALVER'),
        'datasum': header.get('DATASUM'),
        'checksum': header.get('CHECKSUM'),
        'readable': bool(layout and layout['readable']),
        'issues': list(layout['issues']) if layout else [],
        'rows': layout['rows_declared'] if layout else None,
        'row_bytes': layout['row_bytes'] if layout else None,
        'pcount': layout['pcount'] if layout else None,
        'gcount': layout['gcount'] if layout else None,
        'columns': [column_summary(column) for column in layout['columns']]
        if layout else [],
        'data_offset': record['data_offset'],
        'data_span': record['data_span'],
    }
    if not identity['valid']:
        entry['issues'] = entry['issues'] + [issue(
            'error', 'invalid_extension_identity',
            '扩展缺少 OBSID、EXTNAME 或合法的 EXTVER，不能作为可关联身份',
            hdu=record['index'])]
    return entry


def build_manifest(path, exposure=None, source=None):
    """返回一次来源的曝光资料清单。

    清单包含每个二进制表的扩展身份、HDU 位置、行数、列布局和校验摘要；
    HDU 位置只用于回溯，不参与身份判断。文本头不完整、表布局矛盾或缺表
    都记录在 ``issues``/``missing`` 中，而不是抛出“成功但为空”的结果。
    """
    scan = scan_file(path)
    primary_obsid = None
    if scan['hdus']:
        primary_obsid = text(scan['hdus'][0]['header'].get('OBSID')) or None
    extensions = []
    hdus = []
    for record in scan['hdus']:
        hdus.append({'hdu': record['index'], 'kind': record['kind'],
                     'extname': text(record['header'].get('EXTNAME')) or None,
                     'extver': record['header'].get('EXTVER')})
        if record['layout'] is None:
            continue
        extensions.append(_manifest_extension(record, primary_obsid))
    for entry in extensions:
        entry['source'] = dict(source) if source else {
            'path': str(Path(path).resolve()), 'name': Path(path).name,
            'bytes': scan['size']}
    exposures = sorted({entry['identity']['obsid'] for entry in extensions
                        if entry['identity']['obsid']})
    if exposure is not None and exposures and exposure not in exposures:
        exposures = exposures + [exposure]
    issues = []
    if not extensions:
        issues.append(issue('error', 'no_binary_tables', '来源没有二进制表'))
    if not exposures and exposure is None:
        issues.append(issue('error', 'missing_obsid',
                            '来源没有 OBSID，无法确定曝光'))
    if len(exposures) > 1:
        issues.append(issue('error', 'multiple_exposures',
                            '来源包含多个曝光标识', exposures=exposures))
    if exposure is not None:
        primary = exposure
    elif len(exposures) == 1:
        primary = exposures[0]
    else:
        primary = None
    manifest = {
        'source': {'path': str(Path(path).resolve()), 'name': Path(path).name,
                   'bytes': scan['size']},
        'exposure': primary,
        'exposures': exposures,
        'hdus': hdus,
        'extensions': extensions,
        'issues': issues,
    }
    return manifest


def describe_exposure(path, exposure=None, source=None, with_fingerprints=False):
    """清单 + 关联核验；需要接收依据时再显式计算指纹。

    默认只读头部，不读取数据区。``with_fingerprints=True`` 时逐扩展流式
    计算数据摘要，内存占用与文件大小无关。
    """
    from .association import assess_exposure
    from .fingerprint import fingerprint_extension

    manifest = build_manifest(path, exposure=exposure, source=source)
    scan = scan_file(path)
    records = [record for record in scan['hdus'] if record['layout'] is not None]
    by_hdu = {record['index']: record for record in records}
    if with_fingerprints:
        for entry in manifest['extensions']:
            record = by_hdu.get(entry['hdu'])
            if record is None:
                continue
            entry['fingerprint'] = fingerprint_extension(path, record)
            if not entry['fingerprint'].get('data_complete', True):
                entry['issues'] = entry['issues'] + [issue(
                    'error', 'truncated_data', '扩展数据区不完整',
                    hdu=entry['hdu'])]
    assessment = assess_exposure(manifest['exposure'], manifest['extensions'])
    manifest['assessment'] = assessment
    manifest['complete'] = assessment['complete']
    manifest['status'] = assessment['status']
    manifest['missing'] = assessment['missing']
    manifest['issues'] = manifest['issues'] + assessment['issues']
    return manifest
