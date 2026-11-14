# 来源资料约定

来源是 FITS 容器。扩展身份由 OBSID、EXTNAME、EXTVER 三者组成，HDU 排列
位置不能代替身份；EXTNAME 会重复，缺失或重复的身份必须作为可见问题处理。

DETECTIONS 与 QUALITY 通过同一 OBSID 关联；DETECTIONS 的 QUALVER 声明对应
质量版本的 EXTVER，同一曝光可以存在多个 QUALITY 版本。缺少 DETECTIONS、
缺少 QUALVER 指向的 QUALITY 版本、或两表行数不一致时，曝光处于未完整状态，
不能为了“凑成一套”而截断行数或自行挑选版本。

CANDIDATES 是 `PE()` 变长列，QUALITY.MASK 是位数组。空候选（元素数 0）和
NaN 数值含义不同：前者是显式的空数组，后者是无效数值，读取结果都必须保留
原始表示，不能补成零。二进制表列可能带 TSCAL/TZERO、TUNIT、TNULL、TDISP
和 TDIM，读取时同时提供原始值与物理值。

deliveries.json 的 partial 明确说明后续交付不是完整曝光替换：部分补交必须
声明基于哪个已确认版本，只能替换或新增明确列出的扩展。一次补交之后，查询
结果仍会显示尚未补齐的资料。
