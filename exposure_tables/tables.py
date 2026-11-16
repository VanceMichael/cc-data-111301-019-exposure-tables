"""按需读取的 FITS 二进制表访问器。

设计原则：

* 构造时只解析表头，不复制数据区；真正取行/取列时才按字节范围读取。
* 同时保留**原始表示**（磁盘字节的标量值、描述符、位字节）与
  **物理值**（应用 TSCAL/TZERO、TNULL/TUNIT 之后的含义）。
* 无效数值（整数 TNULL 命中、浮点 NaN）标记为 ``valid=False``，
  物理值为 ``null``，绝不补零；真实的零仍然是零。
* 变长列（P/Q）通过堆描述符回溯 heap，位数组（rX）按 FITS 位序展开；
  空候选数组与含 NaN 的标量是两种不同含义，分别保留。
不做任何轨道计算或图像处理，只解释表本身的语义。
"""

import math
import re
from dataclasses import dataclass

import numpy as np
from astropy.io import fits

# FITS 二进制表 TFORM 基础码 -> numpy 磁盘 dtype（一律大端）
_CODE_DTYPE = {
    'L': 'u1', 'B': 'u1', 'A': 'S1',
    'I': '>i2', 'J': '>i4', 'K': '>i8',
    'E': '>f4', 'D': '>f8',
    'C': '>c8', 'M': '>c16',
    'U': '>i2',
}

_TFORM_RE = re.compile(r'^(?P<repeat>\d*)(?P<code>[ABCDEILMJKUX])(?:\((?P<vdim>\d+)\))?$')


@dataclass
class ColumnSpec:
    index: int
    name: str
    tform: str
    repeat: int
    code: str
    vdim: int | None
    byte_offset: int
    item_size: int
    width: int              # 该行中该列占用的字节数（变长列为描述符 8 字节）
    unit: str | None
    bscale: float
    bzero: float
    tnull: int | None
    is_variable: bool
    is_bit: bool
    is_string: bool
    is_complex: bool
    is_integer: bool
    is_float: bool
    is_logical: bool


@dataclass
class _Location:
    path: str
    hdu_index: int
    extname: str
    extver: int | None
    obsid: str | None


def _parse_tform(tform, is_variable):
    match = _TFORM_RE.match(tform.strip().upper())
    if not match:
        raise ValueError(f'不支持的 TFORM: {tform}')
    repeat = int(match.group('repeat') or 1)
    code = match.group('code')
    vdim = int(match.group('vdim')) if match.group('vdim') else None
    if is_variable:
        # P/Q 的 (n) 是每个堆元素的向量维数，repeat 描述符恒为 1
        return 1, code, vdim
    return repeat, code, vdim


class LazyBinTable:
    """对单个 BinTableHDU 的按需读取器。

    生命周期与一个打开的 FITS 文件绑定；推荐使用 :func:`open_table`
    上下文管理，确保文件句柄关闭。
    """

    def __init__(self, hdulist, hdu_index, location: _Location):
        self._hdulist = hdulist
        self._hdu = hdulist[hdu_index]
        if not isinstance(self._hdu, fits.BinTableHDU):
            raise ValueError(f'HDU {hdu_index} 不是二进制表')
        self.location = location
        header = self._hdu.header
        self.nrows = int(header['NAXIS2'])
        self.row_width = int(header['NAXIS1'])
        self._data_offset = self._hdu._data_offset
        self._heap_offset = self._data_offset + self._hdu._theap
        self._heap_size = int(header.get('PCOUNT', 0))
        self._file = self._hdu._file
        self.columns = self._build_columns()

    # ------------------------------------------------------------------ 元数据
    def _build_columns(self):
        hdu = self._hdu
        dtype = hdu.columns.dtype.newbyteorder('>')
        specs = []
        for index, column in enumerate(hdu.columns):
            tform = str(column.format).upper()
            is_variable = tform[0] in ('P', 'Q')
            base_tform = tform[1:] if is_variable else tform
            repeat, code, vdim = _parse_tform(base_tform, is_variable)
            is_bit = code == 'X'
            is_string = code == 'A'
            is_complex = code in ('C', 'M')
            is_integer = code in ('B', 'I', 'J', 'K')
            is_float = code in ('E', 'D')
            is_logical = code == 'L'
            _, field_offset = dtype.fields[column.name][:2]
            if is_variable:
                item_size = np.dtype(_CODE_DTYPE[code]).itemsize
                width = 8  # 描述符固定 8 字节（元素数 + 堆偏移）
            elif is_bit:
                item_size = 1
                width = (repeat + 7) // 8  # rX 占 ceil(r/8) 字节
            else:
                item_size = np.dtype(_CODE_DTYPE[code]).itemsize
                width = item_size * repeat
            bscale = column.bscale if column.bscale is not None else 1
            bzero = column.bzero if column.bzero is not None else 0
            tnull = int(column.null) if column.null is not None else None
            specs.append(ColumnSpec(
                index=index, name=column.name, tform=tform, repeat=repeat,
                code=code, vdim=vdim, byte_offset=int(field_offset),
                item_size=item_size, width=width, unit=column.unit,
                bscale=float(bscale), bzero=float(bzero), tnull=tnull,
                is_variable=is_variable, is_bit=is_bit, is_string=is_string,
                is_complex=is_complex, is_integer=is_integer,
                is_float=is_float, is_logical=is_logical,
            ))
        return specs

    def column(self, name):
        for spec in self.columns:
            if spec.name == name:
                return spec
        raise KeyError(f'列 {name} 不存在')

    # ------------------------------------------------------------------ 字节 IO
    def _read(self, absolute_offset, size):
        return self._file.readarray(offset=absolute_offset, dtype=np.uint8,
                                    shape=(size,))

    def read_row_bytes(self, row_index):
        if not 0 <= row_index < self.nrows:
            raise IndexError(f'行 {row_index} 越界（共 {self.nrows} 行）')
        offset = self._data_offset + row_index * self.row_width
        return np.asarray(self._read(offset, self.row_width), dtype=np.uint8)

    def _read_heap(self):
        if self._heap_size == 0:
            return np.empty(0, dtype=np.uint8)
        return np.asarray(
            self._read(self._heap_offset, self._heap_size), dtype=np.uint8)

    # ------------------------------------------------------------------ 取值
    def read_cell(self, row_index, spec: ColumnSpec):
        """读取一个单元格，返回原始表示与物理值两套结果。"""
        row = self.read_row_bytes(row_index)
        chunk = row[spec.byte_offset: spec.byte_offset + spec.width]
        provenance = {
            'file': self.location.path,
            'hdu_index': self.location.hdu_index,
            'extname': self.location.extname,
            'extver': self.location.extver,
            'obsid': self.location.obsid,
            'row': row_index,
            'column': spec.name,
        }
        if spec.is_variable:
            return self._read_variable(spec, chunk, provenance)
        if spec.is_bit:
            return self._read_bit(spec, chunk, provenance)
        return self._read_fixed(spec, chunk, provenance)

    def _read_fixed(self, spec, chunk, provenance):
        if spec.is_string:
            raw_bytes = bytes(chunk[: spec.repeat])
            # FITS 定长字符串以空格/空字节右填充，去掉填充但保留内容
            raw_value = raw_bytes.decode(
                'ascii', errors='replace').rstrip(' \x00')
            return {
                'raw': raw_value, 'physical': raw_value, 'valid': True,
                'unit': None, 'provenance': provenance,
            }
        shape = (spec.vdim, spec.repeat // spec.vdim) if spec.vdim else (spec.repeat,)
        dtype = np.dtype((_CODE_DTYPE[spec.code], shape)).newbyteorder('>')
        values = np.frombuffer(chunk[: dtype.itemsize], dtype=dtype)
        flat = values.reshape(-1)
        if spec.is_logical:
            raw_list = [int(v) for v in flat]
            phys_list = [self._logical(v) for v in flat]
            return {'raw': raw_list, 'physical': phys_list, 'valid': True,
                    'unit': spec.unit, 'provenance': provenance}
        raw_list = [self._scalar_raw(v, spec) for v in flat]
        physical, valid = self._physical_values(flat, spec)
        return {
            'raw': raw_list if spec.repeat > 1 else raw_list[0],
            'physical': physical if spec.repeat > 1 else physical[0],
            'valid': valid if spec.repeat > 1 else valid[0],
            'unit': spec.unit,
            'bscale': spec.bscale,
            'bzero': spec.bzero,
            'provenance': provenance,
        }

    def _read_bit(self, spec, chunk, provenance):
        # TFORM=rX：r 个有意义的位，占用 ceil(r/8) 字节；按 FITS 位序
        # （每字节 MSB 在前）展开，最后一个字节的填充位不属于该列。
        byte_count = (spec.repeat + 7) // 8
        raw_bytes = [int(b) for b in chunk[:byte_count]]
        bits = []
        for position in range(spec.repeat):
            byte = chunk[position // 8]
            bits.append(bool((int(byte) >> (7 - position % 8)) & 1))
        return {
            'raw': raw_bytes,                 # 磁盘上的字节
            'physical': bits,                 # 仅 r 个有意义的布尔位
            'valid': True,
            'unit': spec.unit,
            'bit_order': 'MSB-first',
            'provenance': provenance,
        }

    def _read_variable(self, spec, descriptor, provenance):
        count = int(np.frombuffer(descriptor[:4], dtype='>i4')[0])
        heap_offset = int(np.frombuffer(descriptor[4:8], dtype='>i4')[0])
        heap = self._read_heap()
        element_dtype = np.dtype(_CODE_DTYPE[spec.code]).newbyteorder('>')
        if count == 0:
            # 空候选：合法的“无候选”，与 NaN 标量含义不同
            elements = np.empty(0, dtype=element_dtype)
        else:
            start = heap_offset
            end = heap_offset + count * element_dtype.itemsize
            if end > len(heap):
                raise ValueError(
                    f'{spec.name} 行 {provenance["row"]} 的堆指针越界')
            elements = np.frombuffer(heap[start:end], dtype=element_dtype)
        if spec.is_string:
            raw_list = [bytes(v).decode('ascii', errors='replace')
                        .rstrip(' \x00') for v in elements]
            physical, valid = raw_list, [True] * len(raw_list)
        else:
            raw_list = [self._scalar_raw(v, spec) for v in elements]
            physical, valid = self._physical_values(elements, spec)
        shape = [count]
        if spec.vdim:
            shape = [count, spec.vdim]
        return {
            'raw': raw_list,
            'physical': physical,
            'valid': valid,
            'unit': spec.unit,
            'bscale': spec.bscale,
            'bzero': spec.bzero,
            'descriptor': {'element_count': count, 'heap_offset': heap_offset},
            'shape': shape,
            'provenance': provenance,
        }

    # ------------------------------------------------------------------ 语义
    @staticmethod
    def _logical(value):
        # FITS: T/F，0/空格视为非法；这里只区分真与非真
        return int(value) == ord('T')

    @staticmethod
    def _scalar_raw(value, spec: ColumnSpec):
        if spec.is_complex:
            return {'real': float(value.real), 'imag': float(value.imag)}
        if spec.is_float:
            fvalue = float(value)
            if math.isnan(fvalue):
                # 原始 NaN 无法进入 JSON，用标记保留“原始表示即无效”的事实
                return {'nan': True}
            return fvalue
        return int(value)

    def _physical_values(self, values, spec: ColumnSpec):
        physical, valid = [], []
        for value in values:
            if spec.is_integer:
                int_value = int(value)
                if spec.tnull is not None and int_value == spec.tnull:
                    physical.append(None)   # 无效：保留 null，不补零
                    valid.append(False)
                    continue
                physical.append(self._scale(int_value, spec))
                valid.append(True)
            elif spec.is_float:
                fvalue = float(value)
                if math.isnan(fvalue):
                    physical.append(None)   # NaN：无效数值，不补零
                    valid.append(False)
                    continue
                physical.append(self._scale(fvalue, spec))
                valid.append(True)
            elif spec.is_complex:
                physical.append({
                    'real': self._scale(float(value.real), spec),
                    'imag': self._scale(float(value.imag), spec),
                })
                valid.append(True)
            else:
                physical.append(self._scalar_raw(value, spec))
                valid.append(True)
        return physical, valid

    @staticmethod
    def _scale(value, spec):
        result = value * spec.bscale + spec.bzero
        # 整数缩放结果若为整数值，保持整数观感
        if float(result).is_integer():
            return int(result)
        return float(result)

    # ------------------------------------------------------------------ 行视图
    def read_row(self, row_index):
        """返回一整行，每个单元格含原始表示、物理值与回溯依据。"""
        return {spec.name: self.read_cell(row_index, spec)
                for spec in self.columns}

    def read_rows(self, start=0, limit=None):
        if limit is None:
            limit = self.nrows - start
        stop = min(start + limit, self.nrows)
        return [self.read_row(i) for i in range(start, stop)]

    def metadata(self):
        return {
            'hdu_index': self.location.hdu_index,
            'extname': self.location.extname,
            'extver': self.location.extver,
            'obsid': self.location.obsid,
            'rows': self.nrows,
            'row_width': self.row_width,
            'heap_bytes': self._heap_size,
            'file': self.location.path,
            'columns': [
                {
                    'name': s.name, 'tform': s.tform, 'unit': s.unit,
                    'bscale': s.bscale, 'bzero': s.bzero, 'tnull': s.tnull,
                    'variable_length': s.is_variable, 'bit_array': s.is_bit,
                } for s in self.columns
            ],
        }


class open_table:  # 小写以模拟上下文管理器习惯用法
    """按需打开归档对象中的某个二进制表扩展。"""

    def __init__(self, path, hdu_index):
        self._path = str(path)
        self._hdu_index = hdu_index
        self._hdulist = None

    def __enter__(self):
        self._hdulist = fits.open(self._path, memmap=True,
                                  lazy_load_hdus=True)
        hdu = self._hdulist[self._hdu_index]
        location = _Location(
            path=self._path, hdu_index=self._hdu_index,
            extname=hdu.name, extver=hdu.header.get('EXTVER'),
            obsid=hdu.header.get('OBSID'),
        )
        return LazyBinTable(self._hdulist, self._hdu_index, location)

    def __exit__(self, exc_type, exc, tb):
        if self._hdulist is not None:
            self._hdulist.close()
        return False
