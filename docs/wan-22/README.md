# WAN-22 产物截图（改前 / 改后）

本目录是 WAN-22（用户侧产物去规则话术 + 面向读者的数据表达与版式）的验收材料：
`policypulse run` 生成的日报页与 `policypulse preview` 生成的复盘页，改前 / 改后各一组，
桌面 1280 与手机 390 各一份。整页截图，可直接对照首屏（hero + KPI）、卡片分区与个股表。

## 同一份数据，只换渲染器

| 文件 | 内容 |
|---|---|
| `report-2026-10-24-before-1280.jpg` / `-before-390.jpg` | 日报，**HEAD（main）版本**渲染器 |
| `report-2026-10-24-after-1280.jpg` / `-after-390.jpg` | 日报，**本 PR 分支**渲染器 |
| `preview-2026-10-23-before-1280.jpg` / `-before-390.jpg` | 复盘页，**HEAD 版本**渲染器 |
| `preview-2026-10-23-after-1280.jpg` / `-after-390.jpg` | 复盘页，**本 PR 分支**渲染器 |

日报取的是仓库里真实跑过的 2026-10-24 那份（`reports/` 自 f5861bd 起不入库，数据取自该提交
之前的产物），还原成同一份 `RunResult` 后分别用两个版本的渲染器出图——21 个政策事件、57 条结论、
3 条深读、31 行个股，结构完全一致，差别只在渲染层。复盘页同理，两版行情数字逐项一致
（38/40 有行情、17 涨 19 跌、平均 −0.05%、中位 −0.07%）。

## 改后怎么看真页面

合并本 PR 后在本机跑一次即可：

```bash
policypulse run                          # → reports/<日期>.html
policypulse preview --date 2026-10-24    # → reports/2026-10-24-preview.html
```

截图由 headless Chromium 整页渲染（1280 CSS px / 390 CSS px），未做裁切或拼接。
