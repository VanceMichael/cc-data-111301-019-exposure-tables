DESCRIPTION = 'FITS 曝光资料的扩展关联与物理值采集'
SAMPLE = 'fixtures/exposure.fits'

import argparse
import json
from pathlib import Path
import tempfile

from .archive import archive_file
from .core import inspect_file
from .manifest import DeliveryError, Registry
from .query import query_exposure


def _print(payload):
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _build_parser():
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    sub = parser.add_subparsers(dest='command')

    sub.add_parser('inspect', help='清点单份 FITS 的 HDU 与扩展身份') \
        .add_argument('path', nargs='?', default=SAMPLE)

    capture = sub.add_parser('capture', help='按内容归档原始字节')
    capture.add_argument('path', nargs='?', default=SAMPLE)
    capture.add_argument('--archive', default='.work/raw')

    deliver = sub.add_parser('deliver', help='登记一次可追溯的资料交付')
    deliver.add_argument('path')
    deliver.add_argument('--exposure', required=True)
    deliver.add_argument('--release', type=int)
    deliver.add_argument('--partial', action='store_true',
                         help='部分补交：只携带部分扩展，不替换整次曝光')
    deliver.add_argument('--note')
    deliver.add_argument('--workdir', default='.work')

    status = sub.add_parser('status', help='查询曝光候选资料、关联与缺口')
    status.add_argument('exposure')
    status.add_argument('--workdir', default='.work')

    sub.add_parser('exposures', help='列出台账中所有曝光及其完整状态') \
        .add_argument('--workdir', default='.work')

    claim = sub.add_parser('claim', help='显式领取一套曝光资料（版本化）')
    claim.add_argument('exposure')
    claim.add_argument('--note')
    claim.add_argument('--workdir', default='.work')

    query = sub.add_parser('query', help='按需展开领取资料的物理值与回溯')
    query.add_argument('exposure')
    query.add_argument('--claim')
    query.add_argument('--row-start', type=int, default=0)
    query.add_argument('--row-limit', type=int)
    query.add_argument('--workdir', default='.work')

    sub.add_parser('demo', help='用内置演练资料走完整流程')
    return parser


def main():
    parser = _build_parser()
    args = parser.parse_args()
    command = args.command

    if command is None:
        parser.print_help()
        return
    if command == 'inspect':
        _print(inspect_file(Path(args.path)))
    elif command == 'capture':
        _print(archive_file(Path(args.path), Path(args.archive)))
    elif command == 'deliver':
        registry = Registry(args.workdir)
        result = registry.ingest_delivery(
            args.path, args.exposure, release=args.release,
            partial=args.partial, note=args.note)
        _print(result)
        if result['status'] == 'rejected':
            raise SystemExit(2)
    elif command == 'status':
        _print(Registry(args.workdir).exposure_status(args.exposure))
    elif command == 'exposures':
        _print(Registry(args.workdir).list_exposures())
    elif command == 'claim':
        try:
            _print(Registry(args.workdir).claim(args.exposure, note=args.note))
        except DeliveryError as exc:
            raise SystemExit(f'领取失败: {exc}')
    elif command == 'query':
        _print(query_exposure(
            Registry(args.workdir), args.exposure, claim_id=args.claim,
            row_start=args.row_start, row_limit=args.row_limit))
    else:
        _print(_run_demo())


def _run_demo():
    """用 fixtures 的两次交付（整包 + 质量部分补交）演示完整流程。"""
    fixtures = Path('fixtures')
    with tempfile.TemporaryDirectory() as directory:
        registry = Registry(directory)
        first = registry.ingest_delivery(
            fixtures / 'exposure.fits', 'exposure-a', release=1)
        second = registry.ingest_delivery(
            fixtures / 'quality-only.fits', 'exposure-a', release=2,
            partial=True)
        claim_one = registry.claim('exposure-a', note='首次领取')
        view = query_exposure(registry, 'exposure-a')
        return {
            '首次交付': first,
            '部分补交': second,
            '曝光状态': registry.exposure_status('exposure-a'),
            '领取': claim_one,
            '按需查询': view,
        }


if __name__ == '__main__':
    main()
