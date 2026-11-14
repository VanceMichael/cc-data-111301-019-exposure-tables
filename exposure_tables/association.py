"""曝光资料关联：把扩展解析成一套可解释的 DETECTIONS/QUALITY 组合。

关联只使用数据里声明的身份：
- 扩展身份 = (OBSID, EXTNAME, EXTVER)，HDU 位置只做回溯；
- DETECTIONS.QUALVER 指向 QUALITY 的 EXTVER。

本模块不会为了“凑成一套”而截断行数、挑选任意版本或把不同来源的扩展
悄悄配对。缺表、身份重复、版本不匹配、行数不一致都会留下可见的未完整
状态。``complete`` 只在这些检查全部通过时为真。
"""

from __future__ import annotations

from .tables import REQUIRED_TABLES, issue


def _by_name(extensions):
    result = {}
    for entry in extensions:
        name = (entry.get('identity') or {}).get('name')
        result.setdefault(name, []).append(entry)
    return result


def _reference(entry, role):
    identity = entry.get('identity') or {}
    return {
        'role': role,
        'key': entry.get('key'),
        'obsid': identity.get('obsid'),
        'name': identity.get('name'),
        'extver': identity.get('extver'),
        'quality_version': entry.get('quality_version'),
        'hdu': entry.get('hdu'),
        'rows': entry.get('rows'),
        'readable': entry.get('readable'),
        'source': entry.get('source'),
        'fingerprint': entry.get('fingerprint'),
    }


def assess_exposure(exposure, extensions):
    """核验一组已解析扩展，返回完整性评估。

    ``extensions`` 的每一项至少包含 identity/key/hdu/rows/readable/
    quality_version/source；来源与指纹会在领取时原样带入。
    """
    issues = []
    named = _by_name(extensions)
    detections = named.get('DETECTIONS', [])
    qualities = named.get('QUALITY', [])
    missing = []

    for name in REQUIRED_TABLES:
        present = named.get(name, [])
        if not present:
            missing.append({'code': 'missing_table', 'table': name,
                            'message': '缺少 {} 表'.format(name)})
    if len(detections) > 1:
        issues.append(issue(
            'error', 'ambiguous_table', '同一曝光存在多个 DETECTIONS 版本，不能自行选择',
            versions=[entry.get('key') for entry in detections]))
    if len(qualities) > 1:
        issues.append(issue(
            'warning', 'multiple_quality_versions',
            '同一曝光存在多个 QUALITY 版本，按 QUALVER 引用',
            versions=[entry.get('key') for entry in qualities]))

    for entry in extensions:
        if not entry.get('readable', False):
            issues.append(issue(
                'error', 'unreadable_extension', '扩展不可读',
                key=entry.get('key'), hdu=entry.get('hdu')))
        if not (entry.get('identity') or {}).get('valid'):
            issues.append(issue(
                'error', 'invalid_extension_identity', '扩展身份不完整',
                key=entry.get('key'), hdu=entry.get('hdu')))

    pairs = []
    referenced = set()
    if len(detections) == 1:
        det = detections[0]
        qualver = det.get('quality_version')
        if qualver is None:
            missing.append({'code': 'missing_qualver', 'table': 'DETECTIONS',
                            'key': det.get('key'),
                            'message': 'DETECTIONS 缺少 QUALVER，无法确定质量版本'})
        else:
            matches = [entry for entry in qualities
                       if (entry.get('identity') or {}).get('extver') == qualver]
            if not matches:
                missing.append({'code': 'missing_quality_version',
                                'table': 'QUALITY', 'extver': qualver,
                                'message': '缺少 DETECTIONS.QUALVER 指向的 QUALITY 版本'})
            elif len(matches) > 1:
                issues.append(issue(
                    'error', 'duplicate_quality_version',
                    'QUALVER 指向的质量版本不唯一',
                    extver=qualver, keys=[entry.get('key') for entry in matches]))
            else:
                qual = matches[0]
                referenced.add(qual.get('key'))
                rows_match = (det.get('rows') == qual.get('rows')
                              and det.get('rows') is not None)
                pairs.append({
                    'detections': _reference(det, 'detections'),
                    'quality': _reference(qual, 'quality'),
                    'quality_version': qualver,
                    'detections_rows': det.get('rows'),
                    'quality_rows': qual.get('rows'),
                    'rows_match': rows_match,
                })
                if not rows_match:
                    issues.append(issue(
                        'error', 'row_count_mismatch',
                        'DETECTIONS 与 QUALITY 行数不一致，不按较短表截断',
                        detections_rows=det.get('rows'),
                        quality_rows=qual.get('rows')))

    for entry in qualities:
        if entry.get('key') not in referenced:
            issues.append(issue(
                'warning', 'unreferenced_quality_version',
                'QUALITY 版本没有被任何 DETECTIONS.QUALVER 引用',
                key=entry.get('key'),
                extver=(entry.get('identity') or {}).get('extver')))

    if len(detections) > 1:
        for entry in detections:
            if entry.get('key') not in {pair['detections']['key'] for pair in pairs}:
                issues.append(issue(
                    'error', 'unpaired_detections',
                    'DETECTIONS 版本没有参与唯一配对', key=entry.get('key')))

    complete = not missing and not any(
        item['severity'] == 'error' for item in issues)
    return {
        'exposure': exposure,
        'complete': complete,
        'status': 'complete' if complete else 'incomplete',
        'extensions': [_reference(entry, _role(entry, referenced))
                       for entry in extensions],
        'pairs': pairs,
        'missing': missing,
        'issues': issues,
    }


def _role(entry, referenced):
    name = (entry.get('identity') or {}).get('name')
    if name == 'DETECTIONS':
        return 'detections'
    if name == 'QUALITY':
        return 'quality' if entry.get('key') in referenced else 'quality-extra'
    return 'other'
