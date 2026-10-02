# 实时数据接入验证手册

当前工具版本：`0.2.0.dev0`。这是可运行的只读验证工具，不是已完成线上数据验收的采集服务或预测模型。完整背景和编辑记录见 [PROJECT_HANDOVER.md](PROJECT_HANDOVER.md)。

## 已取得的证据

| 项目 | 2026-10-02 的实际结果 |
| --- | --- |
| 大陆目录地址 | `https://crm-cn-prd.sushiro.com.cn/wechat/api/2.0/stores` |
| 协议线索 | GET，查询参数 latitude、longitude、numresults；来自锁定的大陆开源客户端源码 |
| 不带凭证的真实测试 | 系统网络工具正常校验证书后 HTTP 401；新客户端使用有效的系统 CA 文件也得到 401 |
| 本机 Python 初次失败 | 默认 CA 环境缺失，证书验证 code 20；使用现有系统公开 CA 文件后连通。没有关闭 TLS 验证 |
| 单店详情线索 | GET `/wechat/api/2.0/getStoreById?storeId=...`；还没拿到授权后的真实目录和门店 ID，因此未调用真实详情 |
| 来源字段语义与刷新周期 | 尚未验证；401 证明该地址回应并要求鉴权，不证明字段、鉴权续期或 30 秒实时能力 |
| 官方小程序 | 用户已在电脑微信打开；当前自动化读取其窗口超时，尚未获得正常请求记录 |
| 临时请求调试 | 用户已批准仅 crm-cn-prd.sushiro.com.cn；用户安装 CA 后 Surge 显示系统已信任，唯一主机名与 HTTPS URL 捕获模式已核对，等待正常门店请求 |

初次测试只输出 HTTP 状态、错误类别和允许的公共字段，没有保存原始错误正文、请求头或凭证。没有尝试参考源码中附带的查询令牌。

首批门店：中关村大融城店、西单店、成都世豪店。待验证清单在 [config/pilot-stores.json](../config/pilot-stores.json)，ID 暂为 null。禁止拿开源测试里的合成 ID 当作真实 ID。

## 运行与离线检查

要求 Python 3.11 或更新版本，运行时不依赖第三方包。以下从仓库根目录执行，macOS / Linux 示例直接使用 src 路径，不需要联网安装依赖。

```sh
PYTHONPATH=src python3 -m sushiwait --help
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

运行两份明确标记的合成快照，整个 replay 流程不联网：

```sh
PYTHONPATH=src python3 -m sushiwait replay --fixture examples/fixtures/store-detail-01.synthetic.json --fixture examples/fixtures/store-detail-02.synthetic.json --db data/local/demo.sqlite3
PYTHONPATH=src python3 -m sushiwait report --db data/local/demo.sqlite3
```

合成门店 900001 不是任何真实门店。示例里 A020 到 A120 的展示变化只生成 added/removed 等可观察差异，不将它标为“100 人过号”，也不输出等待时间。合成与真实样本按 data_origin 分开读取与统计。

## 真实只读查询

未配置授权时，可以做单次匿名连通诊断；当前生产测试结果是 401：

```sh
PYTHONPATH=src python3 -m sushiwait stores --anonymous --match 中关村 --match 西单 --match 世豪
```

正常查询授权需要来自允许的接入方式或用户正常使用小程序的业务流程。`SUSHIWAIT_QUERY_AUTHORIZATION` 由本机环境注入，可为查询 token 或完整 Bearer 值。不要把真实值放进聊天、命令参数、README、Issue、提交或大模型。客户端不会自动读取微信文件、抓取登录态或使用开源代码中的共享 token。

如本地 Python 报 `tls_verification_failed`，可通过 `SUSHIWAIT_CA_FILE` 指定可信 CA 文件；例如本次 macOS 验证使用已有系统文件 `/etc/ssl/cert.pem`。先确认本机文件有效，不将该路径当作跨平台保证。没有 `verify=False` 或证书验证失败后的不安全回退。

授权配置和目录匹配完成后，用真实 ID 采样：

```sh
PYTHONPATH=src python3 -m sushiwait snapshot --store-id VERIFIED_ID --db data/local/pilot.sqlite3
PYTHONPATH=src python3 -m sushiwait collect --store-id VERIFIED_ID --interval 60 --samples 3 --db data/local/pilot.sqlite3
PYTHONPATH=src python3 -m sushiwait report --db data/local/pilot.sqlite3
```

`VERIFIED_ID` 是占位符，必须替换成真实目录核验的数字 ID。验证阶段 collect 限 1–3 店、30–3600 秒周期、每店 1–120 轮；按每轮开始时间安排，若查询耗时超过周期则实际周期变长。请求失败即停止整次采样，不自动重试鉴权、限流或未知结果。30 秒可以配置，但当前没有验证上游允许或实际 30 秒更新。

工具只有目录和单店详情两个固定 GET 端点，无登录、个人号单、取号、预约、取消、重排接口；不跟随重定向，15 秒网络 I/O 超时、2 MiB 响应硬上限。没有守护进程、多实例锁、通知或全天调度。本机同时不要启动多个采集进程；共享采集和持久计划在后续服务器实现。

## 输出与本地存储

命令输出 JSON；数据库仅保存规范化公共观察与安全失败分类，文件在 `data/local/`，默认 `sushiwait.sqlite3`。该目录被 Git 排除。posix 环境新目录 0700、数据库 0600；拒绝符号链接数据库，未知数据库版本不覆盖。

- `schema_version=1`；SQLite `PRAGMA user_version=1`，samples 表保存 run_id、store_id、data_origin、received_at、ok 与规范化 JSON。
- normalized 字段分别记录 present / missing / null / invalid。缺失不会变成 0，缺失队列不会变成空数组。
- 保留四类 groupQueues 的完整字符串数组、顺序与重复情况，不将展示最大号码当作全局游标。
- `raw_wait` 与 `groupQueuesCount` 单位均为 unknown；未经小程序/现场对照，不解释为分钟、人数或已签到桌数。
- `request_started_at`、`received_at`、`elapsed_ms` 是本地请求时间；`source_updated_at=null`，`upstream_freshness=unknown`。
- content_hash 只作用于规范化公共字段；相同 hash 仅表示所保存字段相同，不能证明缓存或源新鲜度。
- 未知字段记录安全键名清单；未知对象、个人字段值和原始响应不落库。如果以后发现有用字段，再核实语义、权限并加入白名单。
- 失败不生成成功快照；HTTP 状态、TLS/网络/业务错误与字段错误区别记录。

## 需要人工配合的下一步

1. 确认官方小程序已进入门店页，截图只用于判断页面状态和字段含义，不能取得请求签名或授权头。
2. 从该用户正常访问产生的请求中，核实实际目的地址、GET/参数、查询鉴权的来源及有效期。优先使用已有正常的诊断能力，或正式接入资料。
3. 本次已取得仅 `crm-cn-prd.sushiro.com.cn` 临时调试的用户确认。最初 Surge 没有 CA，用户安装后界面明确显示系统已信任；这与 Python 默认 CA 缺失是两个独立问题。已核对唯一 MITM 主机名、捕获 URL 模式 `https://crm-cn-prd.sushiro.com.cn/*`，关闭捕获自动额外开启 MITM，设置最多 3 分钟 / 50 MB / 100 请求的临时捕获。当前等待用户重新进入门店产生正常查询，尚不能宣称成功解密或取得有效查询鉴权。凭证仅本机保存，遇证书锁定停止；实验后关闭捕获和临时解密，并记录设置恢复结果。
4. 目录匹配三家店真实 ID，再同步小程序数字与单店响应；记录观测时间、字段值、页面状态、偏差与变化时间。至少区分堂食、预约和签到桌数。
5. 先验证 60 秒，再在允许频率内验证 30 秒；观察鉴权过期、缓存与异常，保存缺失/失败样本，不仅保存成功样本。

实际操作的凭证与原始网络记录留在本机受控位置，公开仓库只记录无秘密的结构、证据与结论。这次用户授权使用自己的账号协助接入；它不包含真实取号、取消或重排。
