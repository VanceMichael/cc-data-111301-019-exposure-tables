# FITS 曝光资料的扩展关联与物理值资料工具

这个 Python 项目用于业务来源资料的检查、扩展关联核验与可追溯的版本化领取。
它不做天体轨道计算或图像处理，也不把来源文件重新写成一版新 FITS。

`exposure_tables/tables.py` 负责只读头部清点与按需按行读取；`manifest.py`
输出曝光资料清单；`association.py` 用 OBSID/EXTNAME/EXTVER/QUALVER 核验
互相对应关系；`store.py` 负责接收、确认、查询与领取；`archive.py` 保存
原文件；`fixtures/` 保存本地业务演练资料；`tests/` 检查上述行为。

## 环境

Python 3.11.2。依赖及其间接依赖固定在 `requirements.txt`；空清单表示只使用标准库。安装只在环境准备阶段执行：

```sh
python3 -m pip install -r requirements.txt
```

## 运行

```sh
python3 -m exposure_tables inspect fixtures/exposure.fits
python3 -m exposure_tables capture fixtures/exposure.fits --archive .work/raw
python3 -m exposure_tables manifest fixtures/exposure.fits
```

`inspect` 是旧的单文件结构预览。归档返回 SHA-256、字节数及原文件名；文件名
不参与内容身份。输入不可识别时命令失败，不产生空的成功结果。

### 曝光资料清单与关联核验

```sh
python3 -m exposure_tables manifest fixtures/exposure.fits
python3 -m exposure_tables manifest fixtures/quality-only.fits
```

清单默认只读取头部，不加载表数据（加 `--fingerprints` 才流式读取数据区计算接收依据）。每个扩展都按 `OBSID|EXTNAME|EXTVER` 识别，
HDU 位置只用于回溯；`EXTNAME` 重复时不会靠位置猜测。`missing` 和 `issues`
会明确列出缺表、缺 `QUALVER` 指向的版本、行数不一致、重复身份、数据区不
完整等问题。只有全部检查通过时 `complete` 才为真，行数不足不会被截断成
“较短表也算完整”。

### 版本化接收与领取

接收与领取是分开的两步，确认之前不影响任何已领取版本：

```sh
python3 -m exposure_tables receive fixtures/exposure.fits \
    --exposure exposure-a --release 1 --by 接收人 --store .work/exposures
python3 -m exposure_tables confirm --exposure exposure-a --release 1 \
    --by 确认人 --store .work/exposures
python3 -m exposure_tables query --exposure exposure-a --store .work/exposures
python3 -m exposure_tables claim --exposure exposure-a --store .work/exposures \
    --start 0 --limit 2 --by 领取人
```

接收依据包括归档对象 SHA-256、扩展头部原文摘要、数据区 SHA-256 和语义指纹
（身份、行数、列定义、缩放/零点/空值等影响解释的头卡），并复核来源声明的
`DATASUM`。确认时会重新流式校验归档对象，不一致就拒绝；领取前再次校验，
避免归档对象被换了内容后仍被领取。

部分补交必须声明它基于哪个已确认版本：

```sh
python3 -m exposure_tables receive fixtures/quality-only.fits \
    --exposure exposure-a --release 2 --partial --base-release 1 \
    --store .work/exposures
```

替换已有扩展时可用 `--replace 旧KEY`，新增扩展用 `--add 新KEY`；没有显式
声明时只允许身份完全相同、同名同版本的一一替换。部分补交如果会使原本完整
的曝光变为未完整，会被拒绝；旧版本快照保留，仍可按 `--release` 领取。

领取时按需读取：清单阶段不读取数据，取数时只 `seek` 到目标行；变长列只按
描述符读取对应 heap 片段，内存占用与当前行和当前数组相关，而不是整张表或
整个文件。二进制表的缩放、零点、单位、空值、位数组和 `P`/`Q` 变长列都会
保留含义：每列同时给出 `raw`、`physical`、`unit`、`scale`、`zero`、`null`
和 `invalid`；无效数值（如 NaN）不会补成零，而是留空 `physical` 并附
`raw_bits`。候选数组仍与所在行号关联，空数组与 NaN 数值是不同状态。

`query --with-rows` 可按需展示某次曝光的候选与对应质量依据；每行都带扩展
HDU、行号、原文件名与归档对象摘要。`claim` 会把领取动作写入
`claims/` 下的 JSONL 记录，用于追溯。

`verify --exposure` 复核某个版本的每个归档对象和扩展摘要是否仍与接收记录
一致。

## 开发检查

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q exposure_tables tests
```

## 本地演示

```sh
python3 -m exposure_tables demo
```

演示实际读取来源资料，打印原始归档摘要、结构预览与曝光清单，使用临时目录
并自动清理。所有示例身份与业务数值均为虚构，不需要外部账号。
