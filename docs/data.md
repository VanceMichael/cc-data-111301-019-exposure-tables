# 来源资料约定

来源是 FITS 容器。DETECTIONS 与 QUALITY 使用 EXTNAME、EXTVER 和 OBSID 关联；DETECTIONS 的 QUALVER 声明对应质量版本。CANDIDATES 为 PE() 变长列，QUALITY.MASK 为位数组；空候选和 NaN 数值含义不同。deliveries.json 的 partial 明确说明后续交付不是完整曝光替换。

## 扩展身份

HDU 的排列位置不是身份，只作为回溯线索。扩展槽位由 `OBSID + EXTNAME + EXTVER` 唯一确定；同一 `EXTNAME` 允许以不同 `EXTVER` 重复出现，同一次交付内槽位重复则判为身份歧义并拒绝。主 HDU（PrimaryHDU）不参与槽位。

## 关联核验

一次曝光至少需要 DETECTIONS 与 QUALITY 两个槽位，且：

- DETECTIONS 的 `QUALVER` 必须能解析到一个 `EXTNAME=QUALITY`、`EXTVER=QUALVER` 的质量槽位；
- 对应的探测表与质量表行数必须一致。行数不符保留可见的未完整状态，绝不按最短长度截断；
- 整包交付（`partial=false`）必须自带全部必需扩展；部分补交（`partial=true`）允许只携带部分槽位，未到位的引用进入曝光状态的 `missing` 列表。

## 交付与领取

- 每份原始文件按 SHA-256 内容寻址归档，文件名不参与身份，因此同名文件不会互相覆盖。
- 交付是只追加的可追溯记录，带单调递增的 release 号。核验不通过的交付状态为 `rejected`，保留问题清单与原始字节，但不替换任何槽位。
- 领取（claim）由人显式发起，按已接受的交付逐槽位组装，每个槽位记录来源 release、摘要、HDU 与原文件名。软件不自行把任意新旧扩展拼成一套；半包产生的未完整领取不会顶替此前的完整领取。

## 表语义

按需读取，不预先把整份资料载入内存。每个单元格同时给出原始表示与物理值：

- `TSCAL/TZERO`（bscale/bzero）产生物理值，原始存储值单独保留；`TUNIT` 单位随列报告；
- 整数 `TNULL` 与浮点 NaN 标记为 `valid=false`、物理值 `null`，绝不补零；真实的零仍然是零；
- 位数组 `rX` 按 MSB-first 展开为布尔位，原始字节同时保留，填充位不输出；
- 变长列 `P/Q` 通过描述符回溯 heap，给出元素数与堆偏移；空候选数组与 NaN 标量是两种不同含义；
- 每个单元格的 provenance 可反查到归档对象、HDU 下标、行与列。
