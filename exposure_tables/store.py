"""版本化接收、确认与按需领取。

接收分为两步：
1. ``receive`` 只做检查并写一份待确认记录，不影响任何已确认版本；
2. ``confirm`` 由人确认后再复核归档对象与指纹，写成可领取的版本。

每次确认都保存完整状态快照，因此旧版本仍可按 release 领取。部分补交必须
声明基准版本和替换关系，且只有在不破坏已有完整曝光时才被接受。领取时按
需从归档对象读取指定行，不复制整份数据。
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import tempfile
from pathlib import Path

from .archive import archive_file
from .association import assess_exposure
from .fingerprint import fingerprint_extension
from .manifest import describe_exposure
from .tables import TableCursor, extension_identity, issue, scan_file, text


SCHEMA = 'exposure-receipt/1'
CLAIM_SCHEMA = 'exposure-claim/1'


class StoreError(ValueError):
    """接收或领取不能安全进行。"""


class ExposureStore:
    def __init__(self, root):
        self.root = Path(root)
        self.objects = self.root / 'objects'
        self.receipts = self.root / 'receipts'
        self.pending = self.root / 'pending'
        self.claims = self.root / 'claims'

    # ---- 路径与原子写 -------------------------------------------------

    @staticmethod
    def _slug(exposure):
        if (exposure and exposure not in ('.', '..') and
                all(char.isalnum() or char in '._-' for char in exposure) and
                not exposure.startswith('..')):
            return exposure
        digest = hashlib.sha256(str(exposure).encode('utf-8')).hexdigest()[:16]
        return '_' + digest

    def _receipt_path(self, exposure, release, pending=False):
        base = self.pending if pending else self.receipts
        return base / self._slug(exposure) / 'release-{}.json'.format(release)

    @staticmethod
    def _write_json(path, payload):
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            'w', encoding='utf-8', dir=path.parent, prefix='.tmp-', delete=False)
        temporary = Path(handle.name)
        try:
            with handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2,
                          sort_keys=True, allow_nan=False)
                handle.write('\n')
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink(missing_ok=True)

    @staticmethod
    def _now():
        return datetime.datetime.now(datetime.timezone.utc).isoformat(
            timespec='seconds')

    def _known_releases(self, exposure):
        directory = self.receipts / self._slug(exposure)
        releases = []
        if directory.exists():
            for path in directory.glob('release-*.json'):
                stem = path.stem.split('-', 1)[-1]
                if stem.isdigit():
                    releases.append(int(stem))
        return sorted(releases)

    def _pending_releases(self, exposure):
        directory = self.pending / self._slug(exposure)
        releases = []
        if directory.exists():
            for path in directory.glob('release-*.json'):
                stem = path.stem.split('-', 1)[-1]
                if stem.isdigit():
                    releases.append(int(stem))
        return sorted(releases)

    def read_receipt(self, exposure, release):
        path = self._receipt_path(exposure, release)
        if not path.exists():
            raise StoreError('没有已确认的版本: {} release {}'.format(exposure, release))
        return json.loads(path.read_text(encoding='utf-8'))

    def read_pending(self, exposure, release):
        path = self._receipt_path(exposure, release, pending=True)
        if not path.exists():
            raise StoreError('没有待确认的版本: {} release {}'.format(exposure, release))
        return json.loads(path.read_text(encoding='utf-8'))

    def latest(self, exposure):
        releases = self._known_releases(exposure)
        if not releases:
            raise StoreError('曝光 {} 没有已确认的版本'.format(exposure))
        return releases[-1]

    # ---- 接收 ---------------------------------------------------------

    def _state_extensions(self, state):
        return list((state or {}).get('extensions') or [])

    def _validate_delivery(self, manifest):
        problems = []
        if not manifest['exposure']:
            problems.append('来源没有可确定的曝光标识')
        seen = {}
        for entry in manifest['extensions']:
            identity = entry.get('identity') or {}
            if not identity.get('valid'):
                problems.append('HDU {} 的扩展身份不完整'.format(entry.get('hdu')))
            key = entry.get('key')
            if key is not None:
                if key in seen:
                    problems.append('扩展身份重复，不能自行选择: {}（HDU {} 与 {}）'.format(
                        key, seen[key], entry.get('hdu')))
                seen[key] = entry.get('hdu')
            if not entry.get('readable'):
                problems.append('HDU {} 的扩展布局不可读'.format(entry.get('hdu')))
            fingerprint = entry.get('fingerprint') or {}
            if fingerprint.get('datasum_verified') is False:
                problems.append('HDU {} 的 DATASUM 与数据不一致'.format(
                    entry.get('hdu')))
            if fingerprint.get('data_complete') is False:
                problems.append('HDU {} 的数据区不完整'.format(entry.get('hdu')))
        if not manifest['extensions']:
            problems.append('来源没有二进制表')
        return problems

    @staticmethod
    def _extension_snapshot(entry):
        return {
            'key': entry.get('key'),
            'identity': entry.get('identity'),
            'hdu': entry.get('hdu'),
            'quality_version': entry.get('quality_version'),
            'rows': entry.get('rows'),
            'row_bytes': entry.get('row_bytes'),
            'readable': entry.get('readable'),
            'datasum': entry.get('datasum'),
            'checksum': entry.get('checksum'),
            'columns': entry.get('columns'),
            'source': entry.get('source'),
            'fingerprint': entry.get('fingerprint'),
        }

    def _map_replacements(self, delivered, replaces, adds, base_state):
        """把旧 key 与本次交付的扩展一一对应；不做隐式挑选。

        ``replaces`` 为 None 时只允许身份完全相同的同名同版本；``adds``
        必须显式列出基准版本中原本不存在的扩展，绝不按名字猜测。
        """
        base_by_key = {entry['key']: entry
                       for entry in self._state_extensions(base_state)}
        remaining = list(delivered)
        mapping = {}
        additions = []
        if replaces is None:
            replaces = [entry['key'] for entry in delivered
                        if entry['key'] in base_by_key]
            unmapped = [entry['key'] for entry in delivered
                        if entry['key'] not in base_by_key]
            if unmapped and not adds:
                raise StoreError(
                    '部分补交包含基准版本中没有的扩展，必须用 adds 显式声明: '
                    '{}'.format(unmapped))
        else:
            replaces = list(dict.fromkeys(replaces))
        unknown = [key for key in replaces if key not in base_by_key]
        if unknown:
            raise StoreError('替换目标不在基准版本中: {}'.format(unknown))
        for old_key in replaces:
            old = base_by_key[old_key]
            old_name = (old.get('identity') or {}).get('name')
            candidates = [entry for entry in remaining
                          if (entry.get('identity') or {}).get('name') == old_name]
            if len(candidates) > 1:
                same_key = [entry for entry in candidates if entry['key'] == old_key]
                if len(same_key) == 1:
                    candidates = same_key
            if len(candidates) != 1:
                raise StoreError(
                    '替换 {} 需要恰好一份同名扩展，实际 {} 份'.format(
                        old_key, len(candidates)))
            chosen = candidates[0]
            mapping[old_key] = chosen['key']
            remaining.remove(chosen)
        adds = list(dict.fromkeys(adds or []))
        for new_key in adds:
            matches = [entry for entry in remaining if entry['key'] == new_key]
            if len(matches) != 1:
                raise StoreError('新增扩展 {} 不在本次交付中'.format(new_key))
            if new_key in base_by_key:
                raise StoreError('新增扩展 {} 已存在于基准版本，应声明为替换'.format(new_key))
            additions.append(new_key)
            remaining.remove(matches[0])
        if remaining:
            raise StoreError('部分补交包含未声明的扩展: {}'.format(
                [entry['key'] for entry in remaining]))
        return mapping, additions

    def _combined_state(self, base_state, delivered, mapping):
        state = {entry['key']: self._extension_snapshot(entry)
                 for entry in self._state_extensions(base_state)}
        for old_key, new_key in mapping.items():
            state.pop(old_key, None)
        for entry in delivered:
            snapshot = self._extension_snapshot(entry)
            state[snapshot['key']] = snapshot
        return {'extensions': sorted(state.values(), key=lambda item: item['key'])}

    def receive(self, path, exposure=None, release=None, partial=False,
                base_release=None, replaces=None, adds=None, note=None,
                received_by=None):
        """检查来源并写入待确认记录；不改变已确认版本。"""
        manifest = describe_exposure(path, exposure=exposure, source=None,
                                     with_fingerprints=True)
        exposure = exposure or manifest['exposure']
        if not exposure:
            raise StoreError('无法确定曝光标识，拒绝接收')
        if manifest['exposures'] and set(manifest['exposures']) - {exposure}:
            raise StoreError('来源包含与目标曝光不一致的标识: {}'.format(
                manifest['exposures']))
        problems = self._validate_delivery(manifest)
        if problems:
            raise StoreError('接收检查未通过:\n- ' + '\n- '.join(problems))

        accepted = self._known_releases(exposure)
        pending_releases = self._pending_releases(exposure)
        used = set(accepted) | set(pending_releases)
        if release is None:
            release = (max(used) if used else 0) + 1
        else:
            release = int(release)
            if release < 1:
                raise StoreError('版本号必须从 1 开始')
            if release in accepted or release in pending_releases:
                raise StoreError('版本 {} 已存在，不覆盖已接收记录'.format(release))
            if accepted and release <= accepted[-1]:
                raise StoreError('版本 {} 早于已确认的最新版本 {}'.format(
                    release, accepted[-1]))
        latest = accepted[-1] if accepted else None

        if partial:
            if latest is None:
                raise StoreError('首次接收不能是部分补交')
            if base_release is None:
                base_release = latest
            base_release = int(base_release)
            if base_release != latest:
                raise StoreError(
                    '部分补交必须基于最新确认版本 {}，收到 {}'.format(latest, base_release))
            base_receipt = self.read_receipt(exposure, base_release)
            base_state = base_receipt['state']
            mapping, additions = self._map_replacements(
                manifest['extensions'], replaces, adds, base_state)
            combined = self._combined_state(base_state, manifest['extensions'], mapping)
            delivery_map = {'mode': 'partial', 'base_release': base_release,
                            'replaces': mapping,
                            'adds': additions,
                            'replaces_source':
                                'declared' if replaces else 'exact-identity'}
        else:
            if base_release is not None or replaces or adds:
                raise StoreError('完整交付不能声明部分替换或新增')
            combined = self._combined_state(None, manifest['extensions'], {})
            delivery_map = {'mode': 'full', 'base_release': None,
                            'replaces': {}, 'adds': [], 'replaces_source': None}

        combined_assessment = assess_exposure(exposure, combined['extensions'])
        if partial and latest is not None:
            base_receipt = self.read_receipt(exposure, latest)
            if base_receipt.get('complete') and not combined_assessment['complete']:
                raise StoreError(
                    '部分补交会使原本完整的曝光变为未完整，拒绝破坏原有可用资料:\n- ' +
                    '\n- '.join([item['message'] for item in
                                 combined_assessment['missing'] +
                                 [entry for entry in combined_assessment['issues']
                                  if entry['severity'] == 'error']]))

        archived = archive_file(path, self.objects)
        object_path = Path(archived['object'])
        source = {'object': archived['sha256'],
                  'sha256': archived['sha256'],
                  'bytes': archived['bytes'],
                  'original_name': Path(path).name}
        for entry in manifest['extensions']:
            entry['source'] = dict(source)
        for entry in combined['extensions']:
            if entry.get('source', {}).get('sha256') is None:
                entry['source'] = dict(source)
        receipt = {
            'schema': SCHEMA,
            'status': 'pending',
            'exposure': exposure,
            'release': release,
            'received_at': self._now(),
            'received_by': received_by,
            'note': note,
            'source': dict(source,
                           received_path=str(Path(path).resolve())),
            'delivery': delivery_map,
            'delivered': [self._extension_snapshot(entry)
                          for entry in manifest['extensions']],
            'acceptance': self._acceptance(manifest, combined_assessment),
            'state': combined,
            'assessment': combined_assessment,
            'complete': combined_assessment['complete'],
            'missing': combined_assessment['missing'],
            'issues': manifest.get('issues', []) + combined_assessment['issues'],
        }
        self._write_json(self._receipt_path(exposure, release, pending=True), receipt)
        return receipt

    @staticmethod
    def _acceptance(manifest, assessment):
        checks = []
        for entry in manifest['extensions']:
            fingerprint = entry.get('fingerprint') or {}
            checks.append({
                'key': entry.get('key'),
                'hdu': entry.get('hdu'),
                'identity': entry.get('identity'),
                'quality_version': entry.get('quality_version'),
                'rows': entry.get('rows'),
                'data_sha256': fingerprint.get('data_sha256'),
                'data_bytes': fingerprint.get('data_bytes'),
                'header_sha256': fingerprint.get('header_sha256'),
                'semantic_sha256': fingerprint.get('semantic_sha256'),
                'datasum': {
                    'declared': fingerprint.get('datasum_declared'),
                    'computed': fingerprint.get('computed_datasum'),
                    'verified': fingerprint.get('datasum_verified'),
                },
                'readable': entry.get('readable'),
            })
        return {
            'policy': 'exposure-tables/receive-1',
            'extensions': checks,
            'complete': assessment['complete'],
            'pairs': assessment['pairs'],
        }

    def confirm(self, exposure, release, confirmed_by=None, note=None):
        """人工确认待接收版本：复核归档对象后再转为可领取版本。"""
        pending = self.read_pending(exposure, release)
        source = pending['source']
        object_path = self.objects / source['sha256']
        if not object_path.exists():
            raise StoreError('归档对象不存在: {}'.format(source['sha256']))
        digest = hashlib.sha256()
        size = 0
        with object_path.open('rb') as reader:
            while block := reader.read(1 << 20):
                digest.update(block)
                size += len(block)
        if digest.hexdigest() != source['sha256'] or size != source['bytes']:
            raise StoreError('归档对象与接收时摘要不一致，拒绝确认')

        verify = describe_exposure(object_path, exposure=exposure,
                                   source={'object': source['sha256'],
                                           'sha256': source['sha256'],
                                           'bytes': source['bytes'],
                                           'original_name': source['original_name']},
                                   with_fingerprints=True)
        recorded = {item['key']: item for item in pending['acceptance']['extensions']
                    if item.get('key')}
        for entry in verify['extensions']:
            prior = recorded.get(entry.get('key'))
            current = entry.get('fingerprint') or {}
            if prior is None:
                raise StoreError('确认时发现接收记录没有包含扩展 {}'.format(entry.get('key')))
            if (prior.get('data_sha256') != current.get('data_sha256') or
                    prior.get('semantic_sha256') != current.get('semantic_sha256')):
                raise StoreError('扩展 {} 的内容与接收依据不一致'.format(entry.get('key')))

        refreshed = dict(pending)
        refreshed.update({'status': 'confirmed',
                          'confirmed_at': self._now(),
                          'confirmed_by': confirmed_by,
                          'confirmation_note': note})
        self._write_json(self._receipt_path(exposure, release), refreshed)
        self._receipt_path(exposure, release, pending=True).unlink(missing_ok=True)
        return refreshed

    def verify(self, exposure, release=None):
        """复核一个已确认版本涉及的每个归档对象及其扩展摘要。"""
        release = self.latest(exposure) if release is None else int(release)
        receipt = self.read_receipt(exposure, release)
        result = {'exposure': exposure, 'release': release, 'objects': [],
                  'issues': []}
        objects = []
        for entry in receipt['state']['extensions']:
            source = entry.get('source') or {}
            if (source.get('sha256') or source.get('object')) not in objects:
                objects.append(source.get('sha256') or source.get('object'))
        for identifier in objects:
            object_path = self.objects / (identifier or '')
            record = {'object': identifier, 'exists': object_path.exists()}
            result['objects'].append(record)
            if not object_path.exists():
                result['issues'].append(issue('error', 'missing_object',
                                              '归档对象不存在', object=identifier))
                continue
            digest = hashlib.sha256()
            size = 0
            with object_path.open('rb') as reader:
                while block := reader.read(1 << 20):
                    digest.update(block)
                    size += len(block)
            record['sha256'] = digest.hexdigest()
            record['bytes'] = size
            expected = [entry for entry in receipt['state']['extensions']
                        if (entry.get('source') or {}).get('sha256') == identifier]
            expected_bytes = (expected[0].get('source') or {}).get('bytes')
            if digest.hexdigest() != identifier:
                result['issues'].append(issue('error', 'object_mismatch',
                                              '归档对象与记录摘要不一致',
                                              object=identifier))
            elif expected_bytes is not None and size != expected_bytes:
                result['issues'].append(issue('error', 'object_size_mismatch',
                                              '归档对象大小与记录不一致',
                                              object=identifier))
            scan = scan_file(object_path)
            found = {}
            for hdu in scan['hdus']:
                if hdu['layout'] is None:
                    continue
                primary = text(scan['hdus'][0]['header'].get('OBSID')) or None
                identity = extension_identity(hdu['header'], primary)
                if identity.get('key'):
                    found.setdefault(identity['key'], hdu)
            for entry in expected:
                fingerprint = (entry.get('fingerprint') or {})
                found_record = found.get(entry['key'])
                if found_record is None:
                    result['issues'].append(issue(
                        'error', 'missing_extension', '归档中缺少扩展',
                        key=entry['key'], object=identifier))
                    continue
                current = fingerprint_extension(object_path, found_record)
                if current.get('data_sha256') != fingerprint.get('data_sha256'):
                    result['issues'].append(issue(
                        'error', 'extension_changed', '扩展数据与版本记录不一致',
                        key=entry['key'], object=identifier))
                if current.get('semantic_sha256') != fingerprint.get('semantic_sha256'):
                    result['issues'].append(issue(
                        'error', 'extension_meaning_changed',
                        '扩展解释依据与版本记录不一致',
                        key=entry['key'], object=identifier))
        result['valid'] = not any(item['severity'] == 'error'
                                  for item in result['issues'])
        return result

    # ---- 查询与领取 ---------------------------------------------------

    def status(self, exposure):
        accepted = self._known_releases(exposure)
        pending = self._pending_releases(exposure)
        releases = []
        for release in accepted:
            receipt = self.read_receipt(exposure, release)
            releases.append({'release': release, 'status': 'confirmed',
                             'received_at': receipt.get('received_at'),
                             'confirmed_at': receipt.get('confirmed_at'),
                             'delivery': receipt.get('delivery'),
                             'complete': receipt.get('complete'),
                             'missing': receipt.get('missing')})
        for release in pending:
            receipt = self.read_pending(exposure, release)
            releases.append({'release': release, 'status': 'pending',
                             'received_at': receipt.get('received_at'),
                             'delivery': receipt.get('delivery'),
                             'complete': receipt.get('complete'),
                             'missing': receipt.get('missing')})
        return {'exposure': exposure, 'accepted': accepted, 'pending': pending,
                'releases': sorted(releases, key=lambda item: item['release'])}

    def query(self, exposure, release=None, include_rows=False, table='DETECTIONS',
              start=0, limit=None):
        """查询某次曝光的候选、质量依据、扩展位置和仍缺资料。

        默认只返回清单与关联结果；``include_rows`` 为真时才按需读取指定行，
        每行都带扩展 HDU、行号、原文件与归档对象摘要。
        """
        release = self.latest(exposure) if release is None else int(release)
        receipt = self.read_receipt(exposure, release)
        state = receipt['state']['extensions']
        assessment = assess_exposure(exposure, state)
        result = {
            'exposure': exposure,
            'release': release,
            'source': receipt['source'],
            'delivery': receipt['delivery'],
            'complete': assessment['complete'],
            'status': assessment['status'],
            'tables': [
                {'key': entry['key'],
                 'role': role,
                 'identity': entry['identity'],
                 'hdu': entry['hdu'],
                 'rows': entry['rows'],
                 'quality_version': entry.get('quality_version'),
                 'object': (entry.get('source') or {}).get('object'),
                 'original_name': (entry.get('source') or {}).get('original_name'),
                 'columns': entry.get('columns'),
                 'fingerprint': {
                     'data_sha256': (entry.get('fingerprint') or {}).get('data_sha256'),
                     'datasum': (entry.get('fingerprint') or {}).get('datasum_declared'),
                     'verified': (entry.get('fingerprint') or {}).get('datasum_verified'),
                     'semantic_sha256': (entry.get('fingerprint') or {}).get('semantic_sha256'),
                 }}
                for entry, role in [
                    (entry, _role_for(entry, assessment))
                    for entry in state]],
            'pairs': assessment['pairs'],
            'missing': assessment['missing'],
            'issues': assessment['issues'],
            'history': self.status(exposure)['releases'],
        }
        if include_rows:
            result['table'] = table
            if state and table in {entry.get('identity', {}).get('name')
                                   for entry in state}:
                result['rows'] = self._read_version_rows(
                    receipt, table=table, start=start, limit=limit, verify=True)
            else:
                result['rows'] = []
        return result

    def _table_entry(self, receipt, table):
        entries = receipt['state']['extensions']
        named = [entry for entry in entries
                 if (entry.get('identity') or {}).get('name') == table]
        if not named:
            raise StoreError('版本中没有表 {}'.format(table))
        if len(named) > 1:
            raise StoreError('版本中表 {} 不唯一，请指定扩展身份'.format(table))
        return named[0]

    def _read_version_rows(self, receipt, table, start=0, limit=None,
                           verify=True):
        """按需读取一个版本中的表行；质量表行数不符时整次失败，不截断。"""
        entry = self._table_entry(receipt, table)
        source = entry.get('source') or {}
        object_path = self.objects / (source.get('sha256') or '')
        if not object_path.exists():
            raise StoreError('归档对象不存在: {}'.format(source.get('sha256')))
        verified = set()
        if verify:
            self._verify_object(object_path, source)
            verified.add(source.get('sha256') or source.get('object'))
        quality_entry = self._quality_for(receipt, entry) if table == 'DETECTIONS' else None
        quality_cursor = None
        if quality_entry is not None:
            quality_source = quality_entry.get('source') or {}
            quality_path = self.objects / (quality_source.get('sha256') or '')
            if not quality_path.exists():
                raise StoreError('归档对象不存在: {}'.format(
                    quality_source.get('sha256')))
            if verify and (quality_source.get('sha256') or
                           quality_source.get('object')) not in verified:
                self._verify_object(quality_path, quality_source)
            quality_cursor = TableCursor(
                quality_path, identity=_cursor_identity(quality_entry),
                start=start, limit=limit,
                source=_cursor_source(quality_source, quality_path,
                                      quality_entry.get('hdu')))
        with TableCursor(object_path, identity=_cursor_identity(entry),
                         start=start, limit=limit,
                         source=_cursor_source(source, object_path,
                                               entry.get('hdu'))) as cursor:
            return self._join_rows(cursor, quality_cursor, quality_entry)

    @staticmethod
    def _join_rows(cursor, quality_cursor, quality_entry):
        if quality_cursor is None:
            return list(cursor.rows())
        if quality_cursor.total_rows != cursor.total_rows:
            raise StoreError(
                'DETECTIONS 与 QUALITY 行数不一致，拒绝按较短表截断: {} vs {}'.format(
                    cursor.total_rows, quality_cursor.total_rows))
        rows = []
        with quality_cursor:
            for row, quality_row in zip(cursor.rows(), quality_cursor.rows()):
                if row['row'] != quality_row['row']:
                    raise StoreError('DETECTIONS 与 QUALITY 行号未能对齐')
                row['quality'] = _quality_basis(quality_row, quality_entry)
                rows.append(row)
        return rows

    def iter_claim_rows(self, exposure, table='DETECTIONS', release=None,
                        start=0, limit=None, allow_incomplete=False):
        """逐行产出领取记录，不在内存里累积整批行。"""
        release = self.latest(exposure) if release is None else int(release)
        receipt = self.read_receipt(exposure, release)
        if receipt.get('status') != 'confirmed':
            raise StoreError('版本尚未确认，不能领取')
        assessment = assess_exposure(exposure, receipt['state']['extensions'])
        if not assessment['complete'] and not allow_incomplete:
            reasons = [item['message'] for item in assessment['missing']]
            reasons += [item['message'] for item in assessment['issues']
                        if item['severity'] == 'error']
            raise StoreError('曝光资料不完整，不能领取:\n- ' + '\n- '.join(reasons))
        entry = self._table_entry(receipt, table)
        source = entry.get('source') or {}
        object_path = self.objects / (source.get('sha256') or '')
        if not object_path.exists():
            raise StoreError('归档对象不存在: {}'.format(source.get('sha256')))
        self._verify_object(object_path, source)
        quality_entry = self._quality_for(receipt, entry) if table == 'DETECTIONS' else None
        quality_cursor = None
        if quality_entry is not None:
            quality_source = quality_entry.get('source') or {}
            quality_path = self.objects / (quality_source.get('sha256') or '')
            if (quality_source.get('sha256') or quality_source.get('object')) != \
                    (source.get('sha256') or source.get('object')):
                self._verify_object(quality_path, quality_source)
            quality_cursor = TableCursor(
                quality_path, identity=_cursor_identity(quality_entry),
                start=start, limit=limit,
                source=_cursor_source(quality_source, quality_path,
                                      quality_entry.get('hdu')))
        with TableCursor(object_path, identity=_cursor_identity(entry),
                         start=start, limit=limit,
                         source=_cursor_source(source, object_path,
                                               entry.get('hdu'))) as cursor:
            if quality_cursor is None:
                yield from cursor.rows()
                return
            if quality_cursor.total_rows != cursor.total_rows:
                raise StoreError(
                    'DETECTIONS 与 QUALITY 行数不一致，拒绝按较短表截断: {} vs {}'.format(
                        cursor.total_rows, quality_cursor.total_rows))
            with quality_cursor:
                for row, quality_row in zip(cursor.rows(), quality_cursor.rows()):
                    if row['row'] != quality_row['row']:
                        raise StoreError('DETECTIONS 与 QUALITY 行号未能对齐')
                    row['quality'] = _quality_basis(quality_row, quality_entry)
                    yield row

    def _claim_result(self, receipt, table, rows, release, start, claimed_by,
                      assessment):
        entry = self._table_entry(receipt, table)
        source = entry.get('source') or {}
        return {
            'schema': CLAIM_SCHEMA,
            'exposure': receipt['exposure'],
            'release': release,
            'table': table,
            'claimed_by': claimed_by,
            'claimed_at': self._now(),
            'object': source.get('sha256') or source.get('object'),
            'original_name': source.get('original_name'),
            'hdu': entry.get('hdu'),
            'extver': (entry.get('identity') or {}).get('extver'),
            'rows': rows,
            'rows_read': len(rows),
            'start': start,
            'basis': {
                'release': release,
                'receipt_confirmed_at': receipt.get('confirmed_at'),
                'extensions': [_claim_basis(item, receipt) for item in
                               receipt['state']['extensions']],
                'complete': assessment['complete'],
                'missing': assessment['missing'],
            },
        }

    def claim(self, exposure, table='DETECTIONS', release=None, start=0,
              limit=None, claimed_by=None, allow_incomplete=False,
              verify_object=True):
        """领取指定版本的行；按需读取，逐行保留来源与质量依据。"""
        release = self.latest(exposure) if release is None else int(release)
        receipt = self.read_receipt(exposure, release)
        if receipt.get('status') != 'confirmed':
            raise StoreError('版本尚未确认，不能领取')
        assessment = assess_exposure(exposure, receipt['state']['extensions'])
        if not assessment['complete'] and not allow_incomplete:
            reasons = [item['message'] for item in assessment['missing']]
            reasons += [item['message'] for item in assessment['issues']
                        if item['severity'] == 'error']
            raise StoreError('曝光资料不完整，不能领取:\n- ' + '\n- '.join(reasons))
        rows = self._read_version_rows(receipt, table, start=start, limit=limit,
                                       verify=verify_object)
        result = self._claim_result(receipt, table, rows, release, start,
                                    claimed_by, assessment)
        self._record_claim(exposure, result)
        return result

    @staticmethod
    def _verify_object(object_path, source):
        if source.get('object') is None and source.get('sha256') is None:
            raise StoreError('扩展缺少归档对象摘要')
        digest = hashlib.sha256()
        size = 0
        with Path(object_path).open('rb') as reader:
            while block := reader.read(1 << 20):
                digest.update(block)
                size += len(block)
        if digest.hexdigest() != (source.get('sha256') or source.get('object')):
            raise StoreError('归档对象与记录摘要不一致，拒绝领取')
        if source.get('bytes') is not None and size != source['bytes']:
            raise StoreError('归档对象大小与记录不一致，拒绝领取')

    def _quality_for(self, receipt, detection_entry):
        qualver = detection_entry.get('quality_version')
        if qualver is None:
            return None
        matches = [entry for entry in receipt['state']['extensions']
                   if (entry.get('identity') or {}).get('name') == 'QUALITY'
                   and (entry.get('identity') or {}).get('extver') == qualver]
        return matches[0] if len(matches) == 1 else None

    def _record_claim(self, exposure, claim):
        self.claims.mkdir(parents=True, exist_ok=True)
        path = self.claims / '{}.jsonl'.format(self._slug(exposure))
        record = {
            'schema': claim['schema'], 'exposure': claim['exposure'],
            'release': claim['release'], 'table': claim['table'],
            'claimed_by': claim['claimed_by'], 'claimed_at': claim['claimed_at'],
            'object': claim['object'], 'hdu': claim['hdu'],
            'extver': claim['extver'], 'start': claim['start'],
            'rows_read': claim['rows_read'],
        }
        with path.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + '\n')

    def claims_for(self, exposure):
        path = self.claims / '{}.jsonl'.format(self._slug(exposure))
        if not path.exists():
            return []
        return [json.loads(line) for line in
                path.read_text(encoding='utf-8').splitlines() if line.strip()]


def _cursor_identity(entry):
    identity = entry.get('identity') or {}
    return {'obsid': identity.get('obsid'), 'name': identity.get('name'),
            'extver': identity.get('extver')}


def _cursor_source(source, object_path, hdu):
    return {'object': source.get('sha256') or source.get('object'),
            'sha256': source.get('sha256') or source.get('object'),
            'original_name': source.get('original_name'),
            'path': str(object_path), 'hdu': hdu}


def _role_for(entry, assessment):
    name = (entry.get('identity') or {}).get('name')
    if name == 'DETECTIONS':
        return 'detections'
    if name == 'QUALITY':
        referenced = {pair['quality']['key'] for pair in assessment['pairs']}
        return 'quality' if entry.get('key') in referenced else 'quality-extra'
    return 'other'


def _quality_basis(row, entry):
    return {
        'key': entry.get('key'),
        'row': row['row'],
        'hdu': entry.get('hdu'),
        'extver': (entry.get('identity') or {}).get('extver'),
        'object': (entry.get('source') or {}).get('sha256'),
        'values': row['values'],
        'issues': row['issues'],
    }


def _claim_basis(entry, receipt):
    fingerprint = entry.get('fingerprint') or {}
    return {
        'key': entry.get('key'), 'hdu': entry.get('hdu'),
        'rows': entry.get('rows'),
        'object': (entry.get('source') or {}).get('sha256'),
        'original_name': (entry.get('source') or {}).get('original_name'),
        'data_sha256': fingerprint.get('data_sha256'),
        'semantic_sha256': fingerprint.get('semantic_sha256'),
        'datasum_verified': fingerprint.get('datasum_verified'),
        'release': receipt.get('release'),
    }
