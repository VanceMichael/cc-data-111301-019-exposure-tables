DESCRIPTION = 'FITS 曝光资料的扩展关联与物理值采集'
SAMPLE = 'fixtures/exposure.fits'

import argparse
import json
import sys
from pathlib import Path
import tempfile

from .archive import archive_file
from .core import inspect_file
from .manifest import describe_exposure
from .store import ExposureStore, StoreError
from .tables import TableCursor


def _store(args):
    return ExposureStore(Path(args.store))


def _read_identity(value):
    if value is None:
        return None
    parts = value.split('|')
    if len(parts) != 3:
        raise SystemExit('扩展身份格式应为 OBSID|EXTNAME|EXTVER: {!r}'.format(value))
    return {'obsid': parts[0], 'name': parts[1], 'extver': int(parts[2])}


def _print(result):
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))


def main(argv=None):
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument('command', choices=[
        'inspect', 'capture', 'demo', 'manifest', 'receive', 'confirm',
        'status', 'query', 'claim', 'verify', 'rows'])
    parser.add_argument('path', nargs='?', default=None)
    parser.add_argument('--archive', default='.work/raw')
    parser.add_argument('--store', default='.work/exposures')
    parser.add_argument('--exposure')
    parser.add_argument('--release', type=int)
    parser.add_argument('--base-release', type=int)
    parser.add_argument('--partial', action='store_true')
    parser.add_argument('--replace', action='append', default=None,
                        help='部分补交中要替换的旧扩展身份 KEY（可重复）')
    parser.add_argument('--add', action='append', default=None,
                        help='部分补交中新增的扩展身份 KEY（可重复）')
    parser.add_argument('--note')
    parser.add_argument('--table', default='DETECTIONS')
    parser.add_argument('--start', type=int, default=0)
    parser.add_argument('--limit', type=int)
    parser.add_argument('--by')
    parser.add_argument('--allow-incomplete', action='store_true')
    parser.add_argument('--with-rows', action='store_true')
    parser.add_argument('--fingerprints', action='store_true',
                        help='manifest 时额外流式计算数据指纹（会读取数据区）')
    parser.add_argument('--skip-object-check', action='store_true')
    args = parser.parse_args(argv)
    if args.command in ('inspect', 'capture', 'demo', 'manifest', 'receive') and args.path is None:
        args.path = SAMPLE
    try:
        if args.command == 'inspect':
            result = inspect_file(Path(args.path))
        elif args.command == 'capture':
            result = archive_file(Path(args.path), Path(args.archive))
        elif args.command == 'demo':
            with tempfile.TemporaryDirectory() as directory:
                result = {'原始资料': archive_file(Path(args.path), Path(directory)),
                          '资料预览': inspect_file(Path(args.path)),
                          '曝光清单': describe_exposure(args.path,
                                                        exposure=args.exposure,
                                                        with_fingerprints=False)}
        elif args.command == 'manifest':
            result = describe_exposure(args.path, exposure=args.exposure,
                                       with_fingerprints=args.fingerprints)
        elif args.command == 'receive':
            result = _store(args).receive(
                Path(args.path), exposure=args.exposure, release=args.release,
                partial=args.partial, base_release=args.base_release,
                replaces=args.replace, adds=args.add, note=args.note,
                received_by=args.by)
        elif args.command == 'confirm':
            if args.exposure is None or args.release is None:
                raise SystemExit('confirm 需要 --exposure 与 --release')
            result = _store(args).confirm(args.exposure, args.release,
                                          confirmed_by=args.by, note=args.note)
        elif args.command == 'status':
            if args.exposure is None:
                raise SystemExit('status 需要 --exposure')
            result = _store(args).status(args.exposure)
        elif args.command == 'query':
            if args.exposure is None:
                raise SystemExit('query 需要 --exposure')
            result = _store(args).query(
                args.exposure, release=args.release, table=args.table,
                include_rows=args.with_rows, start=args.start, limit=args.limit)
        elif args.command == 'verify':
            if args.exposure is None:
                raise SystemExit('verify 需要 --exposure')
            result = _store(args).verify(args.exposure, release=args.release)
        elif args.command == 'rows':
            identity = _read_identity(args.path)
            if identity is None:
                raise SystemExit('rows 需要 PATH 为 OBSID|EXTNAME|EXTVER')
            with TableCursor(args.store, identity=identity, start=args.start,
                             limit=args.limit) as cursor:
                result = cursor.metadata()
                result['rows'] = list(cursor.rows())
        else:  # claim
            if args.exposure is None:
                raise SystemExit('claim 需要 --exposure')
            result = _store(args).claim(
                args.exposure, table=args.table, release=args.release,
                start=args.start, limit=args.limit, claimed_by=args.by,
                allow_incomplete=args.allow_incomplete,
                verify_object=not args.skip_object_check)
    except (StoreError, FileNotFoundError, ValueError) as error:
        print('错误: {}'.format(error), file=sys.stderr)
        return 2
    _print(result)
    return 0


if __name__ == '__main__':
    sys.exit(main())
