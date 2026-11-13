# 来源资料约定

来源是 FITS 容器。DETECTIONS 与 QUALITY 使用 EXTNAME、EXTVER 和 OBSID 关联；DETECTIONS 的 QUALVER 声明对应质量版本。CANDIDATES 为 PE() 变长列，QUALITY.MASK 为位数组；空候选和 NaN 数值含义不同。deliveries.json 的 partial 明确说明后续交付不是完整曝光替换。
