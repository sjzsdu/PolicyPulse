# PolicyPulse

每天抓一次中央与部委的会议/政策通稿，抽政策信号，映射到行业与个股，产出一份
**每条结论都能点回原文**的"潜在受益个股排序"。个人自用、单机、单进程。

> 结论是"政策受益度排序 + 理由"，**不是涨幅预测，不是买卖点**。所有产物自带免责声明。

## 一条命令跑通

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"

policypulse selftest          # 先确认 tongstock / 网络 / LLM 三件事的状态
policypulse run               # 抓取 → 抽取 → 映射 → 打分 → 出日报
```

跑完的产物：

- `reports/YYYY-MM-DD.md` — 日报（主交付）
- `reports/YYYY-MM-DD.json` — 同一批结论的结构化版本
- `reports/YYYY-MM-DD.html` — 单页只读展示
- `data/policypulse.db` — SQLite：documents / signals / signal_ticker / runs …
- `data/snapshots/<run_id>/*.html` — 原文快照

`仓库根目录/reports/2026-10-19.md` 是本机真实跑出来的一份，可以直接打开看效果。

其它命令：

```bash
policypulse sources                    # 列出信息源清单
policypulse show <signal_id>           # 打印一个信号的完整溯源链
policypulse verify --run <run_id>      # 重抓原文核对：一致 / 内容已变 / 链接失效
policypulse run --date 2026-10-09 --lookback-days 7
policypulse run --sources gov_zhengce ndrc_xwfb
```

## 依赖的外部工具

**tongstock**（本机 `/Users/juzhongsun/.local/bin/tongstock`，通达信 TDX 数据）是
A 股基础信息、行业/概念板块与成分股的数据来源，必须可用：

```bash
tongstock block files                      # 确认 TDX 服务器可达
tongstock stockinfo --exchange sh --json   # 个股市值
tongstock block list -f block_gn.dat       # 269 个概念板块
tongstock block show 低空经济 -f block_gn.dat
```

结果会缓存到 `data/cache/`，默认 24 小时内不重复打 TDX；`--refresh-market` 强制刷新。
`block show` 返回的股票名不可信（会显示成指数名），代码里一律用 `stockinfo` / `codes list` 的名称订正。

## 它是怎么跑的

```
sources.yaml 入口页
  → 适配器做链接发现（gov_json_feed：栏目页渲染用的静态 JSON；html_list：列表页正则）
  → 抓详情页，落原文快照 + 正文指纹去重
  → 规则抽取政策信号（无 LLM key 时默认路径；有 key 自动切 LLM，同一套校验兜底）
  → 主题归一（data/theme_alias.csv）→ 板块匹配（精确/别名/包含/模糊≥0.8）
  → 板块成分股展开 → 过滤（ST、市值<30亿、北交所）→ 打分排序 → 落库 → 出日报
```

### 反幻觉硬校验

- 每条信号的 `quote` **必须是原文子串**（归一化空白后比对），不通过则置信度压到 ≤0.4。
- 结论必须带 `source_url` + 原文片段 + `fetched_at`，缺一不产出。
- `policypulse verify` 重抓原文，报告「一致 / 内容已变 / 链接失效 / 抓取失败」三/四种状态。
  原文指纹取**可见文本**，避开脚本注入的计数器噪声。

### 打分

`score = 0.35×力度 + 0.25×主题匹配度 + 0.20×市值档位 + 0.10×板块受益面 + 0.10×政策性质`
权重集中在 `policypulse.yaml`（不存在则用 `Settings` 默认值）。只输出相对排序与理由。

## 没有 LLM key 也能跑

抽取默认是确定性的规则/词表，本机没有任何 LLM key 时全链路照常跑通。
配置下面任一环境变量后自动启用 LLM 抽取，并回退保护：

```bash
export POLICYPULSE_LLM_BASE_URL=https://api.openai.com/v1
export POLICYPULSE_LLM_API_KEY=sk-...
export POLICYPULSE_LLM_MODEL=qwen-plus
```

每次调用会落 `llm_calls` 表，模型或 prompt 变更时 `prompt_version` 递增。

## 加一个信息源

只改 `sources.yaml`，不写深链：

- 政府网栏目：找栏目页渲染用的静态 JSON，用 `gov_json_feed` + `feed_url`。
- 普通列表页：用 `html_list` + `link_pattern`（+ 可选 `reject_pattern`）。

## 测试

```bash
pytest                          # 离线单测（默认）：抽取/映射/打分/存储/报告
pytest -m network               # 真实抓取六个源，验证栏目没改版
pytest -m tongstock             # 校验本机 tongstock 数据可用
```

## 每天自动跑

```cron
30 8 * * * cd /path/to/PolicyPulse && . .venv/bin/activate && policypulse run >> logs/run.log 2>&1
```

## 已知边界（MVP 有意不做）

- 源覆盖面：6 个（国务院×2 + 发改委/财政部/商务部/央行）。工信部站点列表页是
  jpaas 动态渲染，暂未接入；扩源是后续增量。
- 概念板块映射天然偏大市值（板块内按市值取 Top-N），个股层面不做基本面判断。
- 打分是可解释的规则加权，不做价格预测、不给目标价、不接交易。
