"""曝光资料清单、扩展关联核验与版本化领取。

术语：

* **扩展槽位（slot）**：由 ``OBSID + EXTNAME + EXTVER`` 唯一确定。HDU 的排列
  位置不是身份；同一 EXTNAME 可以带不同 EXTVER 重复出现。
* **交付（delivery）**：一次可追溯的接收行为，对应一份原始文件（按 SHA-256
  内容寻址归档）与一个单调递增的 release 号。``partial=true`` 表示这只是
  一次部分补交，不是整次曝光的替换。
* **领取（claim）**：人显式发起的、针对某次曝光的一套资料快照。软件只按
  已登记的交付依据组装槽位，绝不自行把任意新旧扩展拼成一套。

所有状态写入一个 JSON 台账（原子替换），归档对象不可变，历史交付与历史
领取只追加、不改写。缺表或行数不符会留下可见的“未完整”状态，绝不按
最短长度截断，也不让半包破坏原先可用的资料。
"""

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .archive import archive_file
from .core import inspect_file

REQUIRED_EXTNAMES = ('DETECTIONS', 'QUALITY')
LEDGER_NAME = 'ledger.json'


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def _slot_key(extname, extver):
    return f'{extname}[{extver}]'


def _atomic_write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w', encoding='utf-8') as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _load_ledger(workdir):
    path = Path(workdir) / LEDGER_NAME
    if not path.exists():
        return {'deliveries': [], 'claims': [], 'next_release': 1}
    with path.open(encoding='utf-8') as handle:
        return json.load(handle)


@dataclass
class ExtensionRecord:
    obsid: str
    extname: str
    extver: int | None
    hdu_index: int
    rows: int | None
    qualver: int | None
    columns: list
    heap_bytes: int

    @property
    def slot(self):
        return _slot_key(self.extname, self.extver)


def _extract_extensions(path):
    """从一份 FITS 来源提取所有带扩展身份的表（只读表头）。"""
    view = inspect_file(path)
    extensions = []
    for hdu in view['hdus']:
        # 主 HDU（PrimaryHDU）不是扩展，即使它带默认名 PRIMARY 也跳过；
        # 本工具只处理二进制表扩展，图像扩展不参与资料槽位
        if hdu['kind'] == 'PrimaryHDU' or not hdu['columns']:
            continue
        extensions.append(ExtensionRecord(
            obsid=hdu['obsid'],
            extname=hdu['name'],
            extver=hdu['extver'],
            hdu_index=hdu['index'],
            rows=hdu['rows'],
            qualver=hdu['qualver'],
            columns=hdu['columns'],
            heap_bytes=hdu['heap_bytes'],
        ))
    return extensions, view


class DeliveryError(ValueError):
    """交付资料无法成立（结构或身份问题），区别于不可解析的坏文件。"""


def _identity_problems(extensions):
    """槽位身份层面的核验：OBSID 一致、槽位不重复。"""
    problems = []
    obsids = {ext.obsid for ext in extensions}
    if len(obsids) != 1:
        problems.append({
            'code': 'OBSID_MISMATCH',
            'detail': f'交付内 OBSID 不一致: {sorted(str(o) for o in obsids)}',
        })
    elif None in obsids:
        problems.append({
            'code': 'OBSID_MISSING',
            'detail': '存在未声明 OBSID 的扩展',
        })

    seen = {}
    for ext in extensions:
        if ext.slot in seen:
            problems.append({
                'code': 'DUPLICATE_SLOT',
                'detail': (f'槽位 {ext.slot} 在 HDU {seen[ext.slot]} 与 '
                           f'HDU {ext.hdu_index} 重复，无法确定身份'),
            })
        seen[ext.slot] = ext.hdu_index
    return problems


def _completeness_problems(extensions, partial):
    """整包交付必须自带齐全的必需扩展；部分补交允许只带一部分。"""
    problems = []
    if partial:
        return problems
    present_names = {ext.extname for ext in extensions}
    for required in REQUIRED_EXTNAMES:
        if required not in present_names:
            problems.append({
                'code': 'MISSING_EXTENSION',
                'detail': f'整包交付缺少必需扩展 {required}',
            })
    return problems


def _detection_quality_problems(detections, quality_by_slot, allow_gap=False):
    """给定一组探测表与可用质量槽位，核验 QUALVER 引用与行数对应。

    ``allow_gap`` 用于部分补交：质量版本尚未到位属于可见缺口，由曝光
    状态的 missing 列表跟踪，不判失败；但行数不符等硬性冲突始终是问题。
    """
    problems = []
    for det in detections:
        if det.qualver is None:
            problems.append({
                'code': 'QUALVER_MISSING',
                'detail': f'DETECTIONS HDU {det.hdu_index} 未声明 QUALVER',
            })
            continue
        target = _slot_key('QUALITY', det.qualver)
        quality = quality_by_slot.get(target)
        if quality is None:
            if allow_gap:
                continue
            problems.append({
                'code': 'QUALITY_VERSION_MISSING',
                'detail': (f'{_slot_key("DETECTIONS", det.extver)} 引用 '
                           f'{target}，该质量版本尚未收到'),
            })
            continue
        if det.rows != quality.rows:
            problems.append({
                'code': 'ROW_COUNT_MISMATCH',
                'detail': (f'{_slot_key("DETECTIONS", det.extver)} 有 '
                           f'{det.rows} 行，{target} 有 {quality.rows} 行；'
                           '不按最短长度截断'),
            })
    return problems


def verify_delivery(extensions, partial, prior_slots=None):
    """核验一次交付的自洽性，以及与此前已接受槽位的跨交付关联。

    硬性冲突（OBSID 不一致、重复槽位、整包缺表、QUALVER 无法解析、
    行数不符）会让交付被拒绝；被拒交付只留可见记录，不替换任何槽位。
    部分补交允许只带部分槽位，尚未到位的引用属于可见缺口，由曝光状态的
    missing 列表跟踪，不在交付阶段判失败。
    """
    problems = _identity_problems(extensions)
    problems += _completeness_problems(extensions, partial)

    prior_slots = dict(prior_slots or {})
    # 模拟接受后的槽位全集：本次槽位覆盖同名旧槽位
    combined = dict(prior_slots)
    for ext in extensions:
        combined[ext.slot] = ext
    quality_by_slot = {slot: ext for slot, ext in combined.items()
                       if ext.extname == 'QUALITY'}

    # 只核验“本次交付携带的探测表”，避免把既存缺口误算成新交付的问题
    detections = [ext for ext in extensions
                  if ext.extname == 'DETECTIONS']
    problems += _detection_quality_problems(
        detections, quality_by_slot, allow_gap=partial)

    # 本次若只补交质量表，则反过来核验它与既存探测表是否仍然对应。
    # 行数不符属于硬性冲突，即使是部分补交也必须拒绝，不能顶替好数据。
    if not detections:
        prior_detections = [ext for ext in prior_slots.values()
                            if ext.extname == 'DETECTIONS']
        problems += _detection_quality_problems(
            prior_detections, quality_by_slot, allow_gap=False)
    return problems


class Registry:
    """曝光资料台账：归档对象 + 交付/领取 JSON 状态。"""

    def __init__(self, workdir, archive_subdir='raw'):
        self.workdir = Path(workdir)
        self.archive_dir = self.workdir / archive_subdir
        self.ledger_path = self.workdir / LEDGER_NAME

    # ------------------------------------------------------------------ 台账
    def _read(self):
        return _load_ledger(self.workdir)

    def _write(self, ledger):
        _atomic_write_json(self.ledger_path, ledger)

    def list_exposures(self):
        ledger = self._read()
        obsids = sorted({d['obsid'] for d in ledger['deliveries']})
        return {obsid: self.exposure_status(obsid) for obsid in obsids}

    # ------------------------------------------------------------------ 交付
    def ingest_delivery(self, source, exposure, release=None, partial=False,
                        note=None):
        """归档一份来源并登记为一次交付。

        文件先按内容归档（不可变），再做结构核验；核验不通过仍保留交付
        记录与可见问题，状态标为 ``rejected``，不会进入任何领取依据。
        """
        source = Path(source)
        ledger = self._read()

        if release is None:
            release = ledger['next_release']
        for existing in ledger['deliveries']:
            if existing['release'] == release:
                raise DeliveryError(f'release {release} 已被占用')
            if existing['source_name'] == source.name and \
                    existing['obsid'] == exposure:
                raise DeliveryError(
                    f'文件 {source.name} 对曝光 {exposure} 已登记过')

        # 归档先发生；不可变对象即使后续核验失败也可作为排查证据
        archived = archive_file(source, self.archive_dir)

        try:
            extensions, view = _extract_extensions(
                Path(archived['object']))
        except (OSError, ValueError) as exc:
            delivery = self._record_delivery(
                ledger, release, exposure, partial, archived, [],
                accepted=False,
                problems=[{'code': 'UNREADABLE_SOURCE', 'detail': str(exc)}],
                note=note)
            return delivery

        prior_slots = self._latest_slot_records(
            self._accepted_deliveries(ledger, exposure))
        problems = verify_delivery(extensions, partial, prior_slots)
        delivery = self._record_delivery(
            ledger, release, exposure, partial, archived,
            [self._extension_payload(e) for e in extensions],
            accepted=not problems, problems=problems, note=note)
        return delivery

    def _record_delivery(self, ledger, release, exposure, partial, archived,
                         extension_payloads, accepted, problems, note):
        delivery = {
            'release': release,
            'obsid': exposure,
            'partial': bool(partial),
            'source_name': archived['source'],
            'sha256': archived['sha256'],
            'bytes': archived['bytes'],
            'object': archived['object'],
            'received_at': _utc_now(),
            'status': 'accepted' if accepted else 'rejected',
            'problems': problems,
            'extensions': extension_payloads,
            'note': note,
        }
        ledger['deliveries'].append(delivery)
        ledger['next_release'] = max(ledger['next_release'], release + 1)
        self._write(ledger)
        return delivery

    @staticmethod
    def _extension_payload(ext: ExtensionRecord):
        return {
            'slot': ext.slot,
            'extname': ext.extname,
            'extver': ext.extver,
            'hdu_index': ext.hdu_index,
            'obsid': ext.obsid,
            'rows': ext.rows,
            'qualver': ext.qualver,
            'heap_bytes': ext.heap_bytes,
            'columns': ext.columns,
        }

    def _accepted_deliveries(self, ledger, exposure):
        return [d for d in ledger['deliveries']
                if d['obsid'] == exposure and d['status'] == 'accepted']

    @staticmethod
    def _latest_slot_map(deliveries):
        """按 release 顺序，把每次交付携带的槽位覆盖进槽位映射。"""
        slots = {}
        for delivery in deliveries:
            for ext in delivery['extensions']:
                slots[ext['slot']] = {
                    'extension': ext,
                    'delivery_release': delivery['release'],
                    'sha256': delivery['sha256'],
                    'object': delivery['object'],
                    'source_name': delivery['source_name'],
                    'partial_origin': delivery['partial'],
                }
        return slots

    def _latest_slot_records(self, deliveries):
        """同上，但保留 ExtensionRecord 对象用于跨交付核验。"""
        slots = {}
        for delivery in deliveries:
            for payload in delivery['extensions']:
                slots[payload['slot']] = ExtensionRecord(
                    obsid=payload['obsid'], extname=payload['extname'],
                    extver=payload['extver'], hdu_index=payload['hdu_index'],
                    rows=payload['rows'], qualver=payload['qualver'],
                    columns=payload['columns'],
                    heap_bytes=payload['heap_bytes'])
        return slots

    # ------------------------------------------------------------- 曝光状态
    def exposure_status(self, exposure):
        """给出某次曝光的候选资料、关联核验结论与缺口（不截断、不拼接）。"""
        ledger = self._read()
        deliveries = [d for d in ledger['deliveries']
                      if d['obsid'] == exposure]
        if not deliveries:
            raise KeyError(f'没有曝光 {exposure} 的交付记录')
        accepted = self._accepted_deliveries(ledger, exposure)
        rejected = [d for d in deliveries if d['status'] == 'rejected']
        slot_map = self._latest_slot_map(accepted)

        association = self._association_report(slot_map)
        missing = self._missing_slots(slot_map)
        claims = [c for c in ledger['claims'] if c['obsid'] == exposure]
        current_claim = next((c for c in reversed(claims)
                              if c['status'] == 'complete'), None)

        return {
            'obsid': exposure,
            'deliveries': [self._delivery_summary(d) for d in deliveries],
            'rejected_deliveries': [
                {'release': d['release'], 'problems': d['problems']}
                for d in rejected],
            'candidate_slots': sorted(slot_map),
            'slots': self._slot_payloads(slot_map),
            'association': association,
            'missing': missing,
            'complete': not missing and association['ok'],
            'current_claim': (current_claim['claim_id']
                              if current_claim else None),
            'claims': [c['claim_id'] for c in claims],
        }

    @staticmethod
    def _delivery_summary(delivery):
        return {
            'release': delivery['release'],
            'partial': delivery['partial'],
            'status': delivery['status'],
            'source_name': delivery['source_name'],
            'sha256': delivery['sha256'],
            'problems': delivery['problems'],
            'extension_slots': [e['slot'] for e in delivery['extensions']],
            'received_at': delivery['received_at'],
        }

    @staticmethod
    def _slot_payloads(slot_map):
        return [
            {
                'slot': slot,
                'extname': info['extension']['extname'],
                'extver': info['extension']['extver'],
                'rows': info['extension']['rows'],
                'hdu_index': info['extension']['hdu_index'],
                'qualver': info['extension']['qualver'],
                'delivery_release': info['delivery_release'],
                'partial_origin': info['partial_origin'],
                'sha256': info['sha256'],
                'source_name': info['source_name'],
                'object': info['object'],
            }
            for slot, info in sorted(slot_map.items())
        ]

    @staticmethod
    def _missing_slots(slot_map):
        missing = []
        present_names = {info['extension']['extname']
                         for info in slot_map.values()}
        for required in REQUIRED_EXTNAMES:
            if required not in present_names:
                missing.append({'extname': required,
                                'reason': '尚未收到任何版本'})
        return missing

    @staticmethod
    def _association_report(slot_map):
        """跨交付的候选扩展之间是否互相对应。"""
        quality_by_slot = {slot: info['extension']
                           for slot, info in slot_map.items()
                           if info['extension']['extname'] == 'QUALITY'}
        detections = [info['extension'] for info in slot_map.values()
                      if info['extension']['extname'] == 'DETECTIONS']
        # 状态报告处理的是字典负载，转成轻量记录后复用同一套核验规则
        records = []
        for payload in list(quality_by_slot.values()) + detections:
            records.append(ExtensionRecord(
                obsid=payload['obsid'], extname=payload['extname'],
                extver=payload['extver'], hdu_index=payload['hdu_index'],
                rows=payload['rows'], qualver=payload['qualver'],
                columns=payload['columns'],
                heap_bytes=payload['heap_bytes']))
        quality_records = {r.slot: r for r in records
                           if r.extname == 'QUALITY'}
        detection_records = [r for r in records
                             if r.extname == 'DETECTIONS']
        problems = _detection_quality_problems(
            detection_records, quality_records)
        return {'ok': not problems, 'problems': problems}

    # ------------------------------------------------------------------ 领取
    def claim(self, exposure, note=None):
        """人显式领取一次曝光的一套资料；返回完整或未完整的领取记录。

        组装只依据已接受的交付槽位：部分补交带来的槽位替换会被采用，
        其余槽位沿用此前已领取的来源，且每个槽位都记录来源 release 与
        摘要，可追溯。关联不成立或缺表时领取状态为 ``incomplete``，
        不会顶替此前的完整领取。
        """
        ledger = self._read()
        accepted = self._accepted_deliveries(ledger, exposure)
        if not accepted:
            raise DeliveryError(f'曝光 {exposure} 没有可领取的有效交付')

        slot_map = self._latest_slot_map(accepted)
        association = self._association_report(slot_map)
        missing = self._missing_slots(slot_map)
        complete = not missing and association['ok']

        prior = [c for c in ledger['claims']
                 if c['obsid'] == exposure and c['status'] == 'complete']
        prior_claim = prior[-1] if prior else None

        # 关联不成立或缺表即为未完整；半包不会顶替此前的完整领取
        status = 'complete' if complete else 'incomplete'

        claim_id = f'claim-{exposure}-{len(ledger["claims"]) + 1:04d}'
        record = {
            'claim_id': claim_id,
            'obsid': exposure,
            'created_at': _utc_now(),
            'status': status,
            'complete': complete,
            'association': association,
            'missing': missing,
            'note': note,
            'slots': self._claim_slots(slot_map),
            'supersedes': prior_claim['claim_id'] if (
                complete and prior_claim is not None) else None,
        }
        ledger['claims'].append(record)
        self._write(ledger)
        return record

    @staticmethod
    def _claim_slots(slot_map):
        return [
            {
                'slot': slot,
                'extname': info['extension']['extname'],
                'extver': info['extension']['extver'],
                'hdu_index': info['extension']['hdu_index'],
                'rows': info['extension']['rows'],
                'qualver': info['extension']['qualver'],
                'source_release': info['delivery_release'],
                'source_partial': info['partial_origin'],
                'sha256': info['sha256'],
                'object': info['object'],
                'source_name': info['source_name'],
            }
            for slot, info in sorted(slot_map.items())
        ]

    def get_claim(self, claim_id):
        ledger = self._read()
        for claim in ledger['claims']:
            if claim['claim_id'] == claim_id:
                return claim
        raise KeyError(f'领取记录 {claim_id} 不存在')

    def object_path(self, sha256):
        return self.archive_dir / sha256
