"""FITS 曝光资料的扩展关联、版本化领取与物理值采集。

公共入口：

* :func:`core.inspect_file` — 单份 FITS 的 HDU 与扩展身份清点（只读表头）
* :class:`manifest.Registry` — 交付台账、关联核验与版本化领取
* :func:`query.query_exposure` — 按需展开领取资料的物理值与回溯依据
* :class:`tables.open_table` — 单扩展的按需、保留语义的二进制表读取器
"""

from .core import inspect_file
from .manifest import DeliveryError, Registry
from .query import query_exposure
from .tables import open_table

__all__ = [
    'inspect_file',
    'Registry',
    'DeliveryError',
    'query_exposure',
    'open_table',
]
