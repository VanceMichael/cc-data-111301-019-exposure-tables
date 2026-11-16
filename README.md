# FITS 曝光资料的扩展关联与物理值资料工具

这个 Python 项目用于业务来源资料的检查、内容寻址归档、扩展关联核验、版本化领取与按需物理值采集。它不做天体轨道计算或图像处理，只解释 FITS 表本身的结构与语义。

## 模块

- `exposure_tables/core.py`：单份 FITS 的 HDU 与扩展身份清点（OBSID/EXTNAME/EXTVER/QUALVER、列语义），只读表头不读数据区。
- `exposure_tables/archive.py`：原始字节按 SHA-256 内容寻址保存，文件名不参与身份，同名文件不会互相覆盖。
- `exposure_tables/manifest.py`：交付台账。登记可追溯的交付，做槽位身份与关联核验，维护缺口与版本化领取记录。
- `exposure_tables/tables.py`：按需、保留语义的二进制表读取器。区分原始表示与物理值，保留缩放、零点、单位、空值、位数组与 heap 变长列。
- `exposure_tables/query.py`：把领取记录展开为带逐行回溯依据的查询视图。
- `fixtures/` 保存本地业务演练资料，`tests/` 检查行为。

## 环境

Python 3.11.2。依赖及其间接依赖固定在 `requirements.txt`。安装只在环境准备阶段执行：

```sh
python3 -m pip install -r requirements.txt
```

## 运行

```sh
# 清点单份资料的 HDU 与扩展身份
python3 -m exposure_tables inspect fixtures/exposure.fits

# 仅做原始字节归档
python3 -m exposure_tables capture fixtures/exposure.fits --archive .work/raw

# 登记一次整包交付（自动归档 + 关联核验）
python3 -m exposure_tables deliver fixtures/exposure.fits \
    --exposure exposure-a --release 1 --workdir .work

# 登记一次部分补交：只替换携带的槽位，不替换整次曝光
python3 -m exposure_tables deliver fixtures/quality-only.fits \
    --exposure exposure-a --release 2 --partial --workdir .work

# 查看候选资料、关联核验结论与仍然缺失的资料
python3 -m exposure_tables status exposure-a --workdir .work
python3 -m exposure_tables exposures --workdir .work

# 人显式领取一套版本化资料
python3 -m exposure_tables claim exposure-a --workdir .work

# 真正领取后才按需读取大文件，给出物理值与 HDU/行/文件回溯
python3 -m exposure_tables query exposure-a --row-start 0 --row-limit 10 \
    --workdir .work
```

核验不通过的交付以非零退出码失败，并在台账中留下 `rejected` 状态与可见问题；缺表或行数不符表现为未完整状态，不按最短长度截断，也不让半包破坏原先可用的领取。

## 语义保证

- 整数 `TNULL`、浮点 NaN 标为无效（`valid=false`、物理值 `null`），绝不补零；真实的零仍是零。
- 空候选数组（heap 描述符元素数为 0）与 NaN 标量是两种不同含义，分别保留。
- 位数组按 MSB-first 展开，原始字节同时保留；变长列给出 heap 元素数与偏移。
- 每个单元格都带 provenance，可反查到归档对象、HDU 下标、行与列。
- 读取按行/按单元格进行，40MB、200 万行的表读取单行的内存峰值仅数十 KB。

## 开发检查

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q exposure_tables tests
```

## 本地演示

```sh
python3 -m exposure_tables demo
```

演示用 `fixtures/` 的整包交付与质量部分补交走完整流程：归档、核验、状态、领取与按需查询，全部在临时目录完成并自动清理。所有示例身份与业务数值均为虚构，不需要外部账号。
