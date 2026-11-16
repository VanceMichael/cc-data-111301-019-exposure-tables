"""曝光查询：把领取记录按需展开为带物理值与回溯依据的行视图。

只在调用时打开归档对象读取所需 HDU/行，不预先把整份资料读入内存。
每个单元格都能反查到原文件、HDU、行与列；无效数值保持 null，不补零。
"""

from .manifest import Registry
from .tables import open_table


def _slot_for_name(claim, extname):
    return [s for s in claim['slots'] if s['extname'] == extname]


def query_exposure(registry: Registry, exposure, claim_id=None,
                   row_start=0, row_limit=None):
    """查询某次曝光的候选资料、质量依据与逐行物理值。

    默认使用当前完整领取；显式给出 ``claim_id`` 时可回溯任意历史领取，
    包括未完整的部分领取（此时只展开已到位的槽位并列出缺口）。
    """
    status = registry.exposure_status(exposure)
    if claim_id is None:
        claim_id = status['current_claim']
        if claim_id is None:
            return {
                'obsid': exposure,
                'claim_id': None,
                'status': 'no_complete_claim',
                'candidate_slots': status['candidate_slots'],
                'association': status['association'],
                'missing': status['missing'],
                'tables': {},
            }
    claim = registry.get_claim(claim_id)

    tables = {}
    for slot in claim['slots']:
        object_path = registry.object_path(slot['sha256'])
        # 用完整槽位身份做键，避免同名不同 EXTVER 的扩展互相覆盖
        key = slot['slot']
        tables[key] = _read_table(
            object_path, slot, row_start, row_limit)

    return {
        'obsid': exposure,
        'claim_id': claim['claim_id'],
        'claim_status': claim['status'],
        'complete': claim['complete'],
        'association': claim['association'],
        'missing': claim['missing'],
        'candidate_slots': status['candidate_slots'],
        'quality_basis': _quality_basis(claim),
        'tables': tables,
    }


def _read_table(object_path, slot, row_start, row_limit):
    with open_table(object_path, slot['hdu_index']) as table:
        metadata = table.metadata()
        metadata['source_release'] = slot['source_release']
        metadata['source_name'] = slot['source_name']
        metadata['sha256'] = slot['sha256']
        metadata['object'] = str(object_path)
        rows = table.read_rows(row_start, row_limit)
        return {
            'metadata': metadata,
            'row_start': row_start,
            'returned_rows': len(rows),
            'total_rows': table.nrows,
            'rows': rows,
        }


def _quality_basis(claim):
    """说明 DETECTIONS 行所依据的质量版本及其来源。

    存在多个同名质量槽位时全部列出，并标出与探测表 QUALVER 实际绑定的
    那一个，避免把新掩码隐式套到旧探测表上。
    """
    detections = _slot_for_name(claim, 'DETECTIONS')
    qualities = _slot_for_name(claim, 'QUALITY')
    detection_qualver = next(
        (d.get('qualver') for d in detections if d.get('qualver') is not None),
        None)

    candidates = []
    bound = None
    for quality in qualities:
        source = {
            'slot': quality['slot'],
            'extver': quality['extver'],
            'release': quality['source_release'],
            'sha256': quality['sha256'],
            'source_name': quality['source_name'],
            'hdu_index': quality['hdu_index'],
        }
        candidates.append(source)
        if quality['extver'] == detection_qualver:
            bound = source
    return {
        'detection_qualver': detection_qualver,
        'matched': bound is not None,
        'bound_quality': bound,
        'quality_candidates': candidates,
    }
