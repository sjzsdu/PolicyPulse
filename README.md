# PolicyPulse

每天抓一次中央与部委的会议/政策通稿，抽政策信号，映射到行业与个股，产出一份
**每条结论都能点回原文**的"潜在受益个股排序"。个人自用、单机、单进程。

> 结论是"政策受益度排序 + 理由"，**不是涨幅预测，不是买卖点**。所有产物自带免责声明。

## 安装方式（重要）

**只支持 editable 安装**：

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
```

不要 `pip install .`（非 editable）。`sources.yaml` 与 `data/theme_alias.csv` 是随仓库走的
数据文件，`config.py` 按仓库根定位它们，打进 wheel 的非 editable 安装会找不到。
package-data 方案未采用——这个选择是明确的，不是两可。

## 一条命令跑通

```bash
policypulse selftest          # 先确认 tongstock / 网络 / LLM 三件事的状态
policypulse run               # 抓取 → 抽取 → 映射 → 打分 → 出日报
```

常用开关：`--date`、`--lookback-days N`、`--sources <id...>`、`--refresh-market`、
`--no-dedupe`（当天重跑时关掉跨 run 去重）。

跑完的产物：

- `reports/YYYY-MM-DD.md` — 日报（主交付，快照路径写成**相对仓库根**，入库）
- `reports/YYYY-MM-DD.html` — 单页只读展示（入库）
- `reports/YYYY-MM-DD.json` — 同一批结论的结构化版本（**不入库**，含本机绝对路径
  与逐源合规台账 `stats.sources_detail`）
- `data/policypulse.db` — SQLite：documents / signals / signal_ticker / runs …
- `data/snapshots/<run_id>/*` — 原文快照（不入库，后缀跟随原件）
- `inbound/<source_id>/` — 人工下载的原文（目录骨架入库，用户放进去的文件不入库）

`reports/2026-10-19.md` 是本机真实跑出来的一份，可以直接打开看效果。

其它命令：

```bash
policypulse sources                    # 列出信息源清单
policypulse show <signal_id>           # 打印一个信号的完整溯源链
policypulse verify --run <run_id>      # 重抓原文核对：一致 / 内容已变 / 链接失效
policypulse run --date 2026-10-09 --lookback-days 7
policypulse run --sources gov_zhengce xinhua_pbc pbc_inbound
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

## 央行（pbc.gov.cn）怎么拿：robots 全站禁止下的换取法

`http://www.pbc.gov.cn/robots.txt` 是：

```
User-agent: Baiduspider
Allow: /
User-agent: *
Disallow: /
```

即**除了 Baiduspider，一律禁止抓取**。我们不会伪造 UA 去冒充 Baiduspider，也不会用
代理池规避——`sources.yaml` 里的 `pbc_xw` 因此保持 `enabled: false`，`http.py` 的
robots 四档判定会把任何针对该 host 的请求挡在前面。

五个候选方向都实测过（不是估的），结论如下：

| 方向 | 实测结果 | 结论 |
|---|---|---|
| 1. 转载源 · 新华网 `news.cn` | robots `User-Agent: * / Allow: /`；`fortune` 栏目日更，本轮命中 3 篇央行稿 | **采用**，`xinhua_pbc` |
| 1. 转载源 · 中国政府网 `gov.cn` | robots `Allow: /1`（放行）；但 `zhengce/zuixin` 1105 条里央行相关仅 5 条、最新 2023-10；`xinwen/yaowen` 20253 条停在 2023-03 | 不新增源：已有两条 gov.cn 源，转载覆盖陈旧 |
| 1. 转载源 · 人民网 `people.com.cn` | robots `Disallow:`（等于放行）但声明 `Crawl-delay: 120` | **不启用**：一轮 12 篇要 24 分钟。代码已按 `Crawl-delay` 限速，真要开就把 `enabled` 改 true 并接受耗时 |
| 1. 转载源 · 金融时报 `financialnews.com.cn`（央行主管） | robots 404 → `unavailable` 档（可抓，保守 5s + 告警）；但 `yh/` 银行频道停在 2024-04 | 不接入：内容停更 |
| 2. 央行官方公开接口/RSS | `pbc.gov.cn/rss.xml` 404；全站各栏目同 host，一律 `Disallow: /`；`sousuo.www.gov.cn`（政府网搜索接口）也是 `Disallow: /` | **不存在**合规的官方机器接口 |
| 3. 申请抓取授权 | 央行站点未公布任何数据授权/爬虫白名单申请入口，也没有公开的成本与周期说明 | **无公开申请路径**，需要走线下公函，个人自用场景不可持续 |
| 4. 人工/半自动 inbound | 用户本人浏览器下载属个人浏览行为，程序只解析本地文件 | **采用**，`pbc_inbound` |
| 5. AkShare 等第三方数据接口 | 实测可安装可用（`akshare` 1.19.1，MIT）；`macro_china_lpr()` / `macro_china_money_supply()` 能取到数据 | **不接入**：返回的是**数值序列**，没有原文 URL、没有可校验的 quote，过不了「quote 必须是原文子串」和 `verify` 回溯；且引入 pandas 等重依赖，与单机零运维目标不符 |

### 采用的两条路

**① 转载（`xinhua_pbc`，默认启用）** — 新华网财经栏目，robots 实测 `allowed`，
用 `title_pattern` 只收央行口径的稿子（中国人民银行/央行/货币政策/公开市场/降准/LPR/
再贷款/逆回购/MLF…），不把整个财经频道搬进来。

**② 人工入库（`pbc_inbound`，默认启用）** — 你自己下载央行原稿，放进目录即可：

```bash
# 1) 浏览器打开央行栏目，另存为（完整网页 或 只存文本都行）
# 2) 放进这个目录，文件名带日期，并给一个溯源 URL
inbound/pbc_inbound/2026-10-19-公开市场业务交易公告.html
inbound/pbc_inbound/2026-10-19-公开市场业务交易公告.url   # 一行：原文 URL

# 3) 照常跑
policypulse run --sources pbc_inbound
```

「另存为 + 手抄 URL」这一步可以用 `tools/capture_pbc_inbound.py` 省掉：它驱动
**ego-browser 的一次人工会话**（真实 Chromium + 真实 profile，你本人可见、可随时接管），
把页面 HTML 与 `page.url()` 直接写进 `inbound/pbc_inbound/`，文件名里的日期取页面发布时间：

```bash
# URL 由你从央行栏目页肉眼选定后粘过来；工具不做链接发现、不翻页
python3 tools/capture_pbc_inbound.py --url 'https://www.pbc.gov.cn/…/index.html'
python3 tools/capture_pbc_inbound.py --url '<URL1>' --url '<URL2>' --dry-run   # 先看脚本
```

它**不发 HTTP 请求**：请求由浏览器在你的会话里发出，等价于你手动另存；管线对
pbc.gov.cn 依旧零请求。三条硬约束写死在工具里——只取命令行给出的 URL、不伪造 UA/不挂代理、
遇验证码/登录墙 `handOff()` 交还控制权。**不要把它放进定时任务**：无人在场就不该跑，
那就成爬虫了。

2026-10-19 的一次真实会话（本节数字即该次产物）：从
`goutongjiaoliu/113456/113469/index.html`（新闻发布栏目）选了两篇公开稿入库——
《关于金融支持建设北京（京津冀）、上海（长三角）、粤港澳大湾区国际科技创新中心的指导意见》、
《2026年前三季度金融统计数据报告》；随后 `policypulse run` 的 **35 篇 / 87 条信号 /
480 行个股结论**里，央行相关文档 8 篇（`mof_czxw` 2 + `xinhua_pbc` 4 + `pbc_inbound` 2），
`policypulse verify` 为 **33 一致 + 2 一致（本地原件）**，链接失效 0。

溯源 URL 的三个来源（按优先级）：同名 `.url` 伴生文件 → 正文前 5 行的
`原文: https://…` / `来源链接: …` 标记 → HTML 的 `<link rel="canonical">` / `<base href>`。
**三者都没有的文件会被拒收并在告警区点名**——没有 URL 就没有 `source_url`，结论不可回溯，
宁可不出结论。

其它约定：

- 文件**不删不移**，跨 run 去重靠正文指纹，重复跑同一批不会重复抽取。
- `published_at` 优先取文件名里的 `YYYY-MM-DD`，其次正文里的日期；都取不到就留空
  显示「日期未知」，**不拿文件 mtime 冒充发布日期**。
- 目录空着不是故障，只在告警区提示；成功率统计不把本地源算进分母。
- `policypulse verify` 对这类文档**不去重抓源站**（源站禁止抓取），改为与落盘的本地
  原件复算指纹，状态显示「一致（本地原件）」。

### 覆盖不到的部分（不拿劣质数据凑数）

- 央行**政策文件原文**（如《货币政策执行报告》正文）只能靠上面第 ② 条人工入库；
  没有原件就没有这条内容，不做转述、不做摘要替代。
- 「利率市场化 / 公开市场操作」这类**不含产业主题词**的央行稿子，规则抽取能入库、
  能 `verify`，但抽不出带板块映射的利好结论（没有主题词 → 映射不到通达信板块）。
  它们出现在「央行相关文档」计数与「其余信号」表里，不会强行塞进个股排序表。
- 央行**数值型数据**（M2、社融、LPR、公开市场操作量）目前完全不覆盖：现有链路只处理
  「可回溯到原文的政策文本」，数值面板是另一条线，暂无计划。

## 它是怎么跑的

```
sources.yaml 入口页
  → 适配器做链接发现（gov_json_feed：栏目页渲染用的静态 JSON；html_list：列表页正则；
    local_inbound：本地目录，不发网络请求）
  → 抓详情页，落原文快照 + 正文指纹去重
  → 规则抽取政策信号（无 LLM key 时默认路径；有 key 自动切 LLM，同一套校验兜底）
  → 主题归一（data/theme_alias.csv）→ 板块匹配（精确/别名/包含/模糊≥0.8）
  → 板块成分股展开 → 过滤（ST、市值<30亿、北交所）→ 打分排序 → 落库 → 出日报
```

### 抓取合规

- **限速**：单 host 串行，间隔由 `sources.yaml` 的 `rate_limit_seconds` 下发（当前全部 2.0s）。
- **UA**：显式声明 `PolicyPulse/0.1 (personal research)`。
- **robots.txt**：按站读取并遵守，判定分四档（见 `policypulse/http.py`）：

  | 档位 | 含义 | 行为 |
  |---|---|---|
  | `allowed` | 取到规则 | 按 Allow/Disallow 判定，被禁路径直接拒绝 |
  | `disallowed` | 规则明确禁止 | 不抓 |
  | `unavailable` | **拿到 HTTP 响应**但无可用规则（403 / 404 / 302 到 HTML / 内容不是 robots 格式） | 保守策略：仍抓公开页面，该 host 间隔抬到 `robots_unavailable_interval`（默认 5s），原因进日报告警区 |
  | `error` | **没拿到响应**（TLS 校验失败 / 连接失败 / 超时） | **fail-closed，不抓**，显式报错进告警区 |

  关键一点：`error` 绝不能退化成 `unavailable` 照抓——否则遇到真 `Disallow: /`
  的站会静默违规。robots 请求复用正文抓取那个 `httpx.Client`，不另起 `urllib`：
  两套 TLS 栈的 CA / 代理配置不同，同一台机器上可能一个通一个不通，会让档位静默翻转。
- 只抓公开页面，不绕登录 / 验证码 / 付费墙。

### 去重与兜底

- 单次 run 内按正文 `content_hash` 去重；**跨 run 也去重**（指纹已在库则不再重复抽取），
  当天要重跑用 `--no-dedupe`。被跳过的条数写在日报概览里。
- 某栏目在统计窗口内无更新时，会退回去抓入口页最近几条以保证链路有素材；
  这些文档在产物里标注 `窗口外兜底`，概览里也有条数，不会静默混进当天结论。

### 映射口径

概念板块成分由第三方（通达信）维护，宽口径，可能包含非核心主题个股；
本表是政策主题映射结果，不代表个股基本面判断。这句话固定出现在 Markdown「今日概览」
下方和 HTML 顶部，不只写在文档里。

### 反幻觉硬校验

- 每条信号的 `quote` **必须是原文子串**（归一化空白后比对），不通过则置信度压到 ≤0.4。
- 结论必须带 `source_url` + 原文片段 + `fetched_at`，缺一不产出。
- `policypulse verify` 重抓原文，报告「一致 / 内容已变 / 链接失效 / **被限速、暂不可验** / 抓取失败」。
  429 / 503 归到被限速，不算链接失效。原文指纹取**可见文本**，避开脚本注入的计数器噪声。

### 日报口径

Markdown 正文只收「利好 + 有板块映射 + `strength >= min_strength_for_report`」（默认 3）；
被力度门槛滤掉的条数写在概览里，完整结论在同日 JSON 与数据库中，不静默丢。

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
- 转载源只想收某一类稿子：再加 `title_pattern`（对标题做正则过滤）。
- 站点 robots 禁止抓取、但你自己能下载：用 `local_inbound` + `inbound_dir`，
  见上一节「人工入库」。

新增源必须在 `sources.yaml` 里写明该栏目路径与 **robots 实测判定**；日报的
「信息源与合规」表会逐源列出判定结果与本轮文档数，停用源一律标注「不发起任何请求」。

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

- 源覆盖面：联网源 6 个（国务院×2 + 发改委/财政部/商务部 + 新华网财经转载），
  外加 1 个本地 inbound 源（人工下载的央行原稿）。工信部站点列表页是 jpaas 动态渲染，
  暂未接入。
- **央行**：pbc.gov.cn robots 全站禁止，直连源 `pbc_xw` 保持关闭；央行内容走
  「新华网转载 + 人工入库」两条合规路径，详见上文专节。
- 「利率市场化 / 公开市场操作」一类无产业主题词的央行稿子进得来、可 `verify`，
  但映射不到板块，不会出现在个股排序表里。
- 概念板块映射天然偏大市值（板块内按市值取 Top-N），个股层面不做基本面判断。
- 打分是可解释的规则加权，不做价格预测、不给目标价、不接交易。
