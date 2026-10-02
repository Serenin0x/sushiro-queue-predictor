# 实时数据接入验证手册

当前本地工具为 `0.2.0.dev1`，已完成固定新主机适配、API profile 隔离、116 项离线检查和西单实际 CLI 单份快照落库；最近已发布基线仍为 `0.2.0.dev0`。60 秒采样首轮 HTTP 401 后停止，静态鉴权声明到期与失败时序吻合，正常续期未验证；30 秒阶段未执行。字段单位、来源刷新与长期连续采集未验收。完整背景和编辑记录见 [PROJECT_HANDOVER.md](PROJECT_HANDOVER.md)。

## 已取得的证据

| 项目 | 2026-10-02 的实际结果 |
| --- | --- |
| 大陆目录地址 | `https://crm-cn-prd.sushiro.com.cn/wechat/api/2.0/stores` |
| 协议线索 | GET，查询参数 latitude、longitude、numresults；来自锁定的大陆开源客户端源码 |
| 不带凭证的真实测试 | 系统网络工具正常校验证书后 HTTP 401；新客户端使用有效的系统 CA 文件也得到 401 |
| 本机 Python 初次失败 | 默认 CA 环境缺失，证书验证 code 20；使用现有系统公开 CA 文件后连通。没有关闭 TLS 验证 |
| 旧单店详情线索 | legacy GET `/wechat/api/2.0/getStoreById?storeId=...` 来自参考源码；该旧主机的真实详情未验收，不能与已观察的新 gateway 路径混淆 |
| 来源字段语义与刷新周期 | 尚未验证；401 证明该地址回应并要求鉴权，不证明字段、鉴权续期或 30 秒实时能力 |
| Mac 官方小程序 | 用户已正常刷新并完整关闭后重开，数字正常；没有观察到访问候选域名的查询，自动化读取小程序窗口仍超时 |
| Mac 临时请求调试 | 用户已批准仅 crm-cn-prd.sushiro.com.cn；Mac CA 已由用户安装且系统已信任。匿名 Chrome / curl 对照出现在 Surge，仍是 401；没有用户鉴权请求。临时解密与捕获已关闭，单域名规则保留、自动额外 MITM 保持关闭，未开启局域网代理 |
| iPhone 第一轮空捕获 | 单域名短窗已结束；约 19:45 实际看到中关村大融城店公共页面，更新时间 19:44，但保存会话为 0 B / 0 个请求。约 19:47 确认 VPN、MITM、HTTP 捕获与数据包捕获 OFF；Direct、单域名规则、CA 信任保留，自动额外 MITM OFF。没有导出、凭证或真实接口数据，空结果原因未知 |
| iPhone Safari 正对照 | 磁盘过滤改为唯一目标主机关键词后，2026-10-02-200618 会话 4 KB / 1 请求，20:07:02 完成 DIRECT stores GET；浏览器显示缺失 latitude 的 E012 业务错误，HTTP 状态未知。未打开请求头、未导出，证明匿名浏览器捕获可用，不是鉴权或数据成功 |
| iPhone 小程序独立刷新 | 2026-10-02-200908 会话中明确点击西单大悦城店红色刷新，20:09→20:10 页面时间和堂食展示变化，目标捕获仍 0 B。返回时三个临时开关实际 ON，随后全部关闭；没有小程序请求、查询凭证、门店 ID 或真实客户端验收 |
| iPhone 内存主机观察 | 关闭 HTTPS 解密、磁盘与数据包捕获后，约 20:15:41 仅看到活动 HTTPS sapi.sushiro.com.cn:443、DIRECT，与刷新相关联但不是已确定 API。第二次全过程 89 秒超过原≤1分钟方案，已向用户说明，约 20:16 VPN OFF；未看详情、头或导出 |
| 新主机 HTTPS 范围 | 用户已单独批准“允许，仅 sapi.sushiro.com.cn”，自动捕获 1 分钟、一次正常查询、凭证仅本机；唯一新主机配置及本轮捕获已核对，不代表独立鉴权与完整字段成功 |
| 正常 sapi 请求与本机 HAR | 新 sapi 唯一范围捕获一次刷新，2026-10-02-202107 为 5 KB / 1 个 GET；用户交付后核对 20:21:25+08:00、HTTP 200、query 仅 storeId、直接 Store 对象。query/body ID 一致，西单 ID 3004，白名单队列对应页面；Bearer scheme 及三个头名称存在，值不输出。单位/必要头/续期待验 |
| 独立 sapi GET | 约 20:34–20:37 固定 getStoreById?storeId=3004 一次 GET 得到 HTTP 200、182 ms，ID/店名一致，公共值相对 HAR 已变化。默认 CA 曾 tls_error，系统公开 CA 连通后最小头组合为 400，补回正常 User-Agent/Referer/Content-Type 后成功；各头必要性未逐项拆分，不证明源延迟或 30 秒更新 |
| 实际 dev1 CLI 保存 | 20:43:01 单次 GET 成功、319 ms，保存一条 live / miniapp_gateway / 3004；report 实际数据库 schema 2、1 成功 0 失败。wait=35、groupQueuesCount=26 单位未知，booth/mixed=['2063','2064','2065']，reservation/counter=[]、源时间未知。辅助子串检查曾误报，回查确认仅公共白名单，CLI/API 成功不受影响 |
| 有限采样与失败保存 | 20:45:16.961 启动 60 秒 / 2 轮采样，首轮 HTTP 401 / http_error、exit 1，未等待或执行第 2 轮，30 秒阶段未执行，无自动重试。report exit 0、数据库 schema 2，同店同 profile 共 1 成功 1 失败，失败记录安全且来源正确 |
| 静态鉴权到期线索 | 401 后仅本机解析 JWT-like payload 的 exp，声明到期 2026-10-02T12:44:37+00:00（北京时间 20:44:37），20:45:53 检查已过期，与成功和 401 时序吻合；未输出其他 claims/账号标识、未验证签名，不构成唯一因果证明。正常续期尚未捕获或实现 |
| auth-status 安全本机实测 | 仅进程环境注入原 HAR 授权，禁止客户端构造并给无效 CA 路径仍 exit 0；输出声明签发 19:44:37、到期 20:44:37、lifetime 3600、remaining 0 / expired true、source unverified_claim / signature false。无授权值/裸 token 输出，不读其他头或 CA、不建 opener/数据库、不联网或落盘 |

初次测试只输出 HTTP 状态、错误类别和允许的公共字段，没有保存原始错误正文、请求头或凭证。没有尝试参考源码中附带的查询令牌。

首批门店：中关村大融城店、西单店、成都世豪店。西单大悦城店 ID 3004 已在 [config/pilot-stores.json](../config/pilot-stores.json) 回填，api_profile=miniapp_gateway、verification_scope=single_store_read_only；live_data_verified=false 表示连续采集未验收。另两店 ID null，完整目录未验收，不能拿合成 ID 当作真实 ID。

## 门店 ID、查询凭证与逐次生命周期

门店 ID 是标识，不是登录凭证；本次西单 3004 没有到期证据。会过期的是查询鉴权。每份新凭证都要记录声明签发/到期、实际查询成功/失败的时间边界及正常续期结果，不因一次样本宣称固定 TTL。本轮本机仅解析 iat/exp，声明签发为 2026-10-02 19:44:37、到期为 20:44:37（Asia/Shanghai），相差 3600 秒；20:21:25 捕获时剩余 1392 秒（23 分 12 秒）。未验证签名，不把声明当作服务端保证，也不将 20:45 的 401 唯一归因于该声明。

纯本机零网络 auth-status 已实现，并通过主执行者真实配置的安全本机核验和本轮 116 项检查。按 --api-profile 只读对应授权环境变量（legacy 使用 SUSHIWAIT_QUERY_AUTHORIZATION，miniapp_gateway 使用 SUSHIWAIT_GATEWAY_AUTHORIZATION），不读取其他上下文头/CA，不构造网络客户端/opener/数据库，不联网或落盘。只选取和解释 iat/exp，不输出或保存其他 claims/token。已验证运行方法：

```sh
PYTHONPATH=src python3 -m sushiwait auth-status --api-profile miniapp_gateway
PYTHONPATH=src python3 -m sushiwait auth-status --api-profile legacy
```

输出仅包含 ok、api_profile、configured、token_kind、declared_issued_at、declared_expires_at、declared_lifetime_seconds、remaining_seconds、expired、expiry_source、signature_verified。UTC 声明时间与实际请求时间分别理解；当前本机结果声明签发 2026-10-02T11:44:37Z、到期 2026-10-02T12:44:37Z、lifetime 3600、remaining 0 / expired true、expiry_source=unverified_claim / signature_verified=false，授权值和裸 token 不在输出。

iat/exp 独立解释：缺 iat 仍可显示 exp/剩余，缺 exp 只显示签发，未知不推默认 TTL；日期乱序时可显示声明，但计算值为 null、expiry_source=invalid_claim。缺失、opaque 或无效输入的未知字段仍为 null，不当 0 或已失效。输入限 8 KiB ASCII，严格校验重复键、非有限数与 base64/padding，剩余时长向下取整且不小于零。CLI exit 0 / ok 只表示本机检查完成，缺失/opaque/无效/已过期也可 exit 0，不能据退出状态认定授权有效。

该本机工具不验证签名、不续期，也不能判定服务端当前是否接受凭证；实际可用生命周期要与安全 HTTP 结果对照。凭证值只由本机受控流程注入，不作为命令参数、公开记录或模型输入。每份新凭证重复安全时间核对，再在本人已授权正常查询范围验证实际成功/失败和更新过程。

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

命令按固定 API profile 选择目的地址，默认 `legacy`，没有自动探测、跨主机回退或任意 URL 选项：

| Profile | 固定目的地址 | 当前允许只读端点 |
| --- | --- | --- |
| legacy | `https://crm-cn-prd.sushiro.com.cn/wechat/api/2.0` | stores、getStoreById；旧目录匿名测试为 401 |
| miniapp_gateway | `https://sapi.sushiro.com.cn/gateway/wechat/api/2.0` | 仅已观察的 getStoreById；stores 返回 unsupported_endpoint，零网络请求 |

新 gateway 的目录路径没有证据，不能将旧 stores 路径加到新基址。未配置授权时，可在 legacy 做单次匿名连通诊断；此前实际结果为 401：

```sh
PYTHONPATH=src python3 -m sushiwait stores --api-profile legacy --anonymous --match 中关村 --match 西单 --match 世豪
```

正常查询授权来自允许的接入方式或用户正常使用流程，本机注入，不能把值放入聊天、命令参数、README、Issue、提交或模型。gateway 与 legacy 配置独立，不将旧凭证转发到新主机；客户端不自动读取 HAR、微信文件或开源共享 token。新请求本次成功组合保留正常上下文头，各项服务端必需性尚未逐项验证：

| 环境变量 | 使用范围与含义 |
| --- | --- |
| SUSHIWAIT_QUERY_AUTHORIZATION | 仅 legacy 查询 token 或完整 Bearer 值 |
| SUSHIWAIT_GATEWAY_AUTHORIZATION | 仅 gateway 查询 token 或完整 Bearer 值 |
| SUSHIWAIT_GATEWAY_APP_CLIENT | 仅 gateway 的 x-app-client，本机提供 |
| SUSHIWAIT_GATEWAY_APP_CODE | 仅 gateway 的 x-app-code，本机提供 |
| SUSHIWAIT_GATEWAY_USER_AGENT | 仅 gateway 的正常 User-Agent 上下文 |
| SUSHIWAIT_GATEWAY_REFERER | 仅 gateway 的正常 Referer 上下文 |
| SUSHIWAIT_GATEWAY_CONTENT_TYPE | 仅 gateway 的正常 Content-Type 上下文 |
| SUSHIWAIT_CA_FILE | 两 profile 的可信公开 CA 文件，保持证书及主机名验证 |

如本地 Python 报 `tls_verification_failed`，可通过 `SUSHIWAIT_CA_FILE` 指定可信 CA 文件；例如本次 macOS 验证使用已有系统文件 `/etc/ssl/cert.pem`。先确认本机文件有效，不将该路径当作跨平台保证。没有 `verify=False` 或证书验证失败后的不安全回退。

新 profile 选项和实际新 CLI 单店保存已通过本轮核验。凭证与可信 CA 在本机配置后，西单已验证 ID 的单次命令为：

```sh
PYTHONPATH=src python3 -m sushiwait snapshot --api-profile miniapp_gateway --store-id 3004 --db data/local/gateway-pilot.sqlite3
PYTHONPATH=src python3 -m sushiwait report --db data/local/gateway-pilot.sqlite3
```

本轮实际尝试 60 秒 / 2 轮采样，首轮在 20:45:16.961 得到 HTTP 401 后立即停止，没有第 2 轮或周期等待；30 秒阶段未执行，60/30 秒采样均未验收。report 成功读取同店同 profile 的 1 成功与 1 失败。到期静态凭证不再重试；先在用户已授权的本人正常查询流程内核实鉴权更新和有效期，再恢复下列有界验证。第二条命令仅为续期核验后的示例，不是本轮已执行记录：

```sh
PYTHONPATH=src python3 -m sushiwait collect --api-profile miniapp_gateway --store-id 3004 --interval 60 --samples 2 --db data/local/gateway-pilot.sqlite3
PYTHONPATH=src python3 -m sushiwait collect --api-profile miniapp_gateway --store-id 3004 --interval 30 --samples 2 --db data/local/gateway-pilot.sqlite3
```

legacy 的其他真实店 ID 须经对应目录核对后再使用：

```sh
PYTHONPATH=src python3 -m sushiwait snapshot --api-profile legacy --store-id VERIFIED_ID --db data/local/pilot.sqlite3
PYTHONPATH=src python3 -m sushiwait collect --api-profile legacy --store-id VERIFIED_ID --interval 60 --samples 3 --db data/local/pilot.sqlite3
PYTHONPATH=src python3 -m sushiwait report --db data/local/pilot.sqlite3
```

`VERIFIED_ID` 是占位符，必须替换成真实目录核验的数字 ID。验证阶段 collect 限 1–3 店、30–3600 秒周期、每店 1–120 轮；按每轮开始时间安排，若查询耗时超过周期则实际周期变长。请求失败即停止整次采样，不自动重试鉴权、限流或未知结果。30 秒可以配置，但当前没有验证上游允许或实际 30 秒更新。

工具只允许两个固定 profile 的已知只读 GET，gateway 仅详情、目录禁止；无登录、个人号单、取号、预约、取消或重排接口。不跟随重定向或读取环境代理，15 秒 I/O 超时、2 MiB 响应上限、失败不自动重试。没有守护进程、多实例锁、通知或全天调度，本机不要同时运行多个采集进程。

## 电脑试点批量与后期同步展示

用户要求在电脑快捷批量测试门店，取得有效本人查询授权并核实目标 ID 后有节奏执行，不需要逐店手机抓包。当前 collect 可重复传 --store-id，但最多 1–3 店、每店 1–120 轮，首个失败停止；这是试点批量，全国目录和全国批量仍待实现/验收。同一 profile 的多店权限也须核实，不能从西单一次成功推断所有门店均可访问。以下两个 ID 都是占位符，必须先换成对应 profile 已核实的真实 ID；当前另外两家试点仍为 null，本轮没有执行这个多店示例：

```sh
PYTHONPATH=src python3 -m sushiwait collect --api-profile miniapp_gateway --store-id VERIFIED_ID_A --store-id VERIFIED_ID_B --interval 60 --samples 1 --db data/local/gateway-pilot.sqlite3
```

后期本项目小程序需要同步显示/刷新所选门店当前堂食与预约叫号，复用服务端共享采集，让用户减少在两个小程序之间来回切换。仍按有限展示集合保存与显示，分清请求接收时间和未知的源更新时间，明确刷新失败/过期状态，不把旧数据或失败当作无人排队。此项属于 R23，须等 R18 的真实字段、刷新与连续接入齐备后才实现前端；当前没有服务端持续调度或小程序展示功能。

## 输出与本地存储

命令输出 JSON；数据库仅保存规范化公共观察与安全失败分类，文件在 `data/local/`，默认 `sushiwait.sqlite3`。该目录被 Git 排除。posix 环境新目录 0700、数据库 0600；拒绝符号链接数据库，未知数据库版本不覆盖。

- 规范化快照 `schema_version=1` 保持；它与数据库和 report 的版本独立。SQLite `PRAGMA user_version=2`，样本保存 run_id、store_id、data_origin、api_profile、received_at、ok 与规范化 JSON；本轮 116 项离线检查通过，实际新 CLI 使用 schema 2 数据库成功保存。
- 观察、目录项与变化输出顶层 api_profile，仅允许 legacy / miniapp_gateway，不保存任意来源 URL；端点固定映射由客户端负责。compute_change 只有同店、同真实/合成来源、同 API profile 才比较，报告也按三者分组。
- 写打开旧 v1 数据库在显式事务中为旧成功/失败行补 legacy 列与 JSON 标签，保留行 ID、run_id 与内容；冲突 profile 或坏 JSON 拒绝并回滚。只读打开 v1 不迁移，使用虚拟 legacy 视图；report 的 schema_version=2 表示输出格式，database_schema_version 单独反映磁盘库版本。
- normalized 字段分别记录 present / missing / null / invalid。缺失不会变成 0，缺失队列不会变成空数组。
- 保留四类 groupQueues 的完整字符串数组、顺序与重复情况，不将展示最大号码当作全局游标。
- `raw_wait` 与 `groupQueuesCount` 单位均为 unknown；未经小程序/现场对照，不解释为分钟、人数或已签到桌数。waitTimeCounter / waitTimeCap 另按有符号整数保存 presence/value/unit=unknown，拒绝 bool，保留 -1 等原值；不猜测 -1 的禁用含义或正值的分钟单位。
- waitTimeCounter / waitTimeCap 的缺失、null、invalid 分别记录，公共哈希与标量变化覆盖两字段；旧快照缺新键按 missing/None 比较，不当 0、不修改历史 JSON。有符号等待字段新增 4 项回归；加入本机 auth-status 后，主执行者 Python 3.12.8 最终全套 116 项通过，快照 schema_version 仍为 1。
- `request_started_at`、`received_at`、`elapsed_ms` 是本地请求时间；`source_updated_at=null`，`upstream_freshness=unknown`。
- content_hash 只作用于规范化公共字段；相同 hash 仅表示所保存字段相同，不能证明缓存或源新鲜度。
- 未知字段记录安全键名清单；未知对象、个人字段值和原始响应不落库。如果以后发现有用字段，再核实语义、权限并加入白名单。
- 失败不生成成功快照；HTTP 状态、TLS/网络/业务错误与字段错误区别记录。

## 需要人工配合的下一步

1. 确认官方小程序已进入门店页，明确点击底部刷新并记录前后页面时间和展示集合，不能只靠进店或切店假定已发新查询。截图只用于判断页面状态和字段含义，不能取得请求签名或授权头。
2. 从该用户正常访问产生的请求中，核实实际目的地址、GET/参数、查询鉴权的来源及有效期。优先使用已有正常的诊断能力，或正式接入资料。
3. 本次已取得仅 `crm-cn-prd.sushiro.com.cn` 临时调试的用户确认。最初 Mac Surge 没有 CA，用户安装后界面明确显示系统已信任；这与 Python 默认 CA 缺失是两个独立问题。每轮核对唯一 MITM 主机名、捕获 URL 模式 `https://crm-cn-prd.sushiro.com.cn/*`，关闭捕获自动额外开启 MITM，使用最多 3 分钟 / 50 MB / 100 请求的临时捕获。第一轮空结果后曾恢复初始设置；第二轮用户正常刷新、第三轮完整重开小程序后仍未看到小程序请求。匿名浏览器对照的页面被浏览器工具阻止，但其网络请求出现在 Surge；经本机代理的匿名 curl 正常 TLS 得到 401，也出现在列表。捕获路径可见不等于正常小程序接入已打通。Mac 当前临时解密与 HTTP 捕获均关闭，唯一主机和捕获规则保留供后续核对。恢复“自动额外 MITM”被自动审批拒绝，原因是扩大其他主机范围，因此保持关闭，未绕过拒绝。凭证未取得，用户安装的 Mac 证书保留，系统信任由用户管理；遇证书锁定停止。
4. 新 sapi 单店已捕获、独立 HTTP 200 并经实际 dev1 CLI 保存，gateway 适配、profile 隔离和 116 项检查通过。60 秒采样首轮 401 后已停止；先在本人已授权的正常查询流程核实鉴权更新、有效期与必要头，不能跨主机发旧凭证、使用共享令牌或猜 stores/refresh 端点。单次成功不等于连续采集验收。
5. 目录匹配三家店真实 ID，再同步小程序数字与单店响应；记录观测时间、字段值、页面状态、偏差与变化时间。至少区分堂食、预约和签到桌数。
6. 正常续期核验后先验证 60 秒，再在允许频率内验证 30 秒；观察鉴权过期、缓存与异常，保存缺失/失败样本，不仅保存成功样本。当前 401 后没有继续请求或自动重试，不能用过期静态凭证宣称全天访问。

实际操作的凭证与原始网络记录留在本机受控位置，公开仓库只记录无秘密的结构、证据与结论。这次用户授权使用自己的账号协助接入；它不包含真实取号、取消或重排。

## 手机端验证路线（正常请求与单份 CLI 快照已核验，连续采集受鉴权阻碍）

用户批准新 sapi 后，正常 GET、HAR 白名单页面对应、独立 HTTP 200 与 dev1 新 CLI 单份保存均完成；固定 profile、来源隔离和 116 项检查通过。60 秒采样首轮 401 后停止，30 秒阶段未执行，静态鉴权声明到期与时序吻合，正常续期、必要头、单位和源刷新频率未验收。手机 VPN、MITM、HTTP 捕获与数据包捕获均 OFF，Default 直接连接、sapi 规则、CA 信任及服务端验证保留，自动额外 MITM OFF；凭证与原始 HAR 留私有位置，不进公开仓库或模型，Mac 局域网代理未启用。[Surge 原理说明](https://manual.nssurge.com/book/understanding-surge/en/)、[MITM 核验说明](https://kb.nssurge.com/surge-knowledge-base/faq/common-faqs)

已通过手机实际页面截图核对并保存的设置：

- MITM 主机：唯一 `sapi.sushiro.com.cn` 启用，旧 `crm-cn-prd.sushiro.com.cn` 保留但禁用。
- 请求记录的内存捕获过滤器：“包含关键词”唯一 `sapi.sushiro.com.cn` 启用，元数据阶段的 `sushiro.com.cn` 与旧 crm 关键词禁用。
- 磁盘 HTTP 过滤器：“包含关键词”唯一 `sapi.sushiro.com.cn` 启用；旧 crm 关键词及 `https://crm-cn-prd.sushiro.com.cn/*` 表达式保留但禁用，仅 HTTP/HTTPS 请求开启。
- 自动额外 MITM：OFF；本轮磁盘自动限制改为 1 分钟并核验，50 MB / 100 请求保持，其他原有过滤项未改。
- 跳过服务端证书校验：OFF；MITM HTTP2：OFF；自动屏蔽 QUIC：原有 ON 保持。
- 临时运行开关：手机 VPN、MitM、HTTP 捕获、数据包捕获均 OFF。已取得本机正常 sapi 请求、查询上下文与西单 ID，独立单次详情成功；仍未验证续期、频率和连续采集。

镜像辅助功能树只显示 Mac 窗口，没有 iOS 控件文本，操作依据实时截图坐标并重新截图验证保存结果；动画中的短暂空页或过渡页不能作为成功证据。设置截图只留本机，初始无关微信画面已立即切离，没有留存或公开聊天、私人页面内容。

最初用户两次报告已安装，但信任设置仅显示系统证书存储区与资产版本两行，没有 Surge 信任开关；系统 VPN 与设备管理列表也没有本轮 CA，Surge 仍只有生成入口。主执行者约 19:34 点击带箭头的“生成新的 CA 证书”入口，实际立即生成 CA，没有独立确认向导，已明确告知用户该实际行为；保存后重新打开确认同一 CA 持续存在。证书名称、序号和私钥不公开。用户随后在手机完成系统安装，约 19:36 报告安装好了，并报告已安装与开启完全信任；系统列表已出现本轮 CA，但 Surge 等待动画结束后的稳定页仍显示已安装未信任，不能仅按用户报告将完全信任写为已核验。点击 Surge 打开系统设置、Spotlight 输入与粘贴没有完成有效导航；重新观察发现镜像因用户拿起手机中止，未绕过中止继续操作。

约 19:39–19:40 用户再次回复“OK我启用了”后，镜像恢复，实际系统证书信任设置显示本轮根证书开关绿色 ON，返回 Surge 的稳定页面也明确显示系统已信任。至此安装与实际信任均已核验，先前差异已解决；主执行者没有替用户开启信任。此结果只说明证书就绪，尚没有捕获或数据验收。

描述文件下载、系统安装和根证书完全信任是三个独立步骤，本轮都已完成，无需重新安装。正规系统安装入口是设置首页“已下载描述文件” → 安装，或通用 → VPN 与设备管理；仅有 VPN 配置不代表 CA 已安装。Apple 说明下载后 8 分钟内未安装的描述文件会自动删除。安装后须由用户到设置 → 通用 → 关于本机 → 证书信任设置开启根证书的 SSL 完全信任；没有相应信任开关时，应先核对额外证书是否确实安装。完全信任会使手机信任 Surge 调试证书，解密范围仍限唯一寿司郎域名，不代替用户开启信任，不传输 p12 私钥或猜测安装网址。[Apple 描述文件安装](https://support.apple.com/en-hk/102400)、[根证书信任](https://support.apple.com/en-us/102390)

Surge 已安装未信任阶段及当前运行状态的设置截图只留本机，没有保存描述文件或关于本机私人信息截图，没有查看其他描述文件内容，不公开私人描述文件名称、设备标识或本机绝对路径。

第一轮约 19:41 启动手机本机 VPN，没有新的系统权限提示，约 19:42 开启 MITM 与 HTTP 捕获。19:42:48 核对系统 UTC 换算时间后，请用户完整重开官方小程序进入中关村店或其他试点，在 19:45:30 前看到正常数字后锁屏回复。用户回复数字正常，后澄清手机没锁但镜像可查看；约 19:45 主执行者实际看到中关村大融城店公共“当前排队情况”，页面最后更新时间为 19:44：

| 页面公共展示 | 当时内容 |
| --- | --- |
| 排队取号即将带位 | 1819 / 1823 / 1829 |
| 提前预约 | 7391 / --- / --- |
| 号码段说明 | 1–6999 为排队，7000–7999 为预约 |
| 签到与过号说明 | 到店需签到，否则不叫号；过号保留 15 分钟；未签到号码不叫但仍过号，过号 15 分钟后作废；过号后 15 分钟内签到将顺延 5 桌顺序叫号 |
| 座位说明 | 2 人位卡座有空可带位 |

这只是中关村大融城店当时的公共页面文字，仍需现场核对业务执行，不泛化为全部大陆门店规则、不保证宽限、不直接用于模型假定。页面更新时间尚未对应 API 源更新时间字段；没有保存个人号单，页面正常也不能证明捕获或接口接入成功。

约 19:46 返回 Surge，初始动画短暂显示旧的捕获 ON，稳定页面实际已自动 OFF；随后关闭 MITM 与本机 VPN，约 19:47 稳定蓝色启动按钮及三个捕获相关开关 OFF 已确认。保存会话名为 `2026-10-02-194240`，列表显示 0 B，进入后明确为 0 个请求，未导出。Direct、单域名规则及 CA 信任仍保留，未全部恢复，自动额外 MITM OFF；不推断空结果由接口、流量路径或过滤器导致。

约 19:51 用户要求自行开启并刷新几次，已交接同主机小程序刷新及 Safari stores 匿名 GET 步骤。用户报告小程序和 Safari 均为 0；约 19:57 主执行者实际看到手机 Safari 在目标域名显示缺失 latitude 参数的公开 E012 业务错误，没有 HTTP 状态证据。约 20:04 会话列表 `194240` / `195216` / `195358` / `200009` 均为 0 B，其中 195216 与 195358 是用户自行试验，开关时序未受主执行者监督；200009 约 20:00 启动，但上下文恢复延迟使 Safari 重载到约 20:03，确切秒数相对 3 分钟截止未知，不能将其当作有效窗口内正对照。稳定页捕获自动 OFF 后，主执行者关闭剩余 MITM 与 VPN。

E0013 阶段的官方排障资料指出过滤器可能隐藏请求，但当时未找到 iOS 磁盘通配符匹配对象的明确官方规范。约 20:05–20:06 手机过滤页底部帮助实际说明“包含关键词”匹配 URL、“通配符”匹配 URL 模式、前缀“-”用于排除主机；这是后续 UI 证据，不能写成此前官方文档已经给出该规范。主执行者据实际页面将磁盘过滤改为唯一目标主机关键词，禁用旧 URL 通配符表达式并保存；内存原关键词、仅 HTTP/HTTPS、自动额外 MITM OFF 与数据包捕获 OFF 未改。新过滤正对照成立也不能证明此前空结果唯一由过滤模式导致。[Surge 排障说明](https://kb.nssurge.com/surge-knowledge-base/guidelines/troubleshooting)

会话 `2026-10-02-200618` 启动手机 VPN、MITM 与 HTTP 捕获。20:06 首次 Safari 重载被用户切换应用中止，重新观察后约 20:07 成功点击，回 Surge 实际确认三个临时开关仍 ON，随后逐项关闭。会话 4 KB，仅一个已完成请求：20:07:02 GET `https://crm-cn-prd.sushiro.com.cn/wechat/api/2.0/stores`，DIRECT，响应 782 B、上传 490 B。浏览器 E012 仍可见，HTTP 状态未知；未打开请求头、未导出 HAR。这个在有效窗口内的匿名 Safari 正对照证明当前浏览器捕获可用，不能当作查询授权、真实目录或字段成功。用户随后确认在同窗手动点击过小程序刷新，记录仍只有 Safari。

主执行者随后在同过滤下开启仅小程序会话 `2026-10-02-200908`，明确点击西单大悦城店公共页面底部红色“刷新”，并核对稳定页面：

| 页面时间 | 排队取号即将带位 | 提前预约 |
| --- | --- | --- |
| 20:09，点击刷新前 | 1875 / 1884 / 1885 | 7421 / 7410 / --- |
| 20:10，点击刷新后 | 1898 / 1900 / 1902 | 7421 / 7410 / --- |

返回 Surge 时捕获、MITM 与 VPN 均实际 ON，随后全部关闭，小程序专用会话仍为 0 B。只能记录展示集合变化，不以号码最大值或差值推断全局游标、处理桌数或过号人数；页面时间尚未对应到 API 源时间，也不泛化该门店规则。没有请求头读取、HAR 导出、查询凭证、真实客户端验收或真实门店 ID。

为定位实际请求主机，用户对不超过 1 分钟、只在内存筛选 sushiro.com.cn 子域名主机与时间的方案回复“允许，这里你可以自由发挥”；方案关闭 HTTPS 解密、磁盘与数据包捕获，不读取请求内容。第一次约 20:12–20:14 启动后点击刷新，但第 25 秒仍在开启、第 34 秒才稳定绿色、第 44 秒停止，刷新与实际接管时序未建立；只局部看到首号 1915，不构成完整快照。黄色捕获卡“最近”是 6 个磁盘历史会话，停机后工具最近请求入口禁用，没有新主机证据，不能据此宣称其他域名不存在。

第二次约 20:14–20:16 启动，工具最近请求入口约第 27 秒可用。刷新前西单大悦城店完整页面时间为 20:13，堂食 1915 / 1921 / 1923，预约 7421 / 7410 / ---；约 20:15 点击刷新后只局部看到首号 1942，不补写另外两号。工具列表仅有活动 HTTPS `sapi.sushiro.com.cn:443`，20:15:41、DIRECT，只观察主机和时间，未打开详情、请求头或导出，磁盘、MITM、数据包均 OFF。这是与刷新关联的候选主机，尚未证明是叫号 API。

第二次读取列表时约第 69 秒、点击停止时约第 89 秒，超过原≤1分钟方案；实际 VPN 接管起点未知，不能将启动延迟解释为全过程符合上限。主执行者已向用户透明说明，约 20:16 蓝色启动按钮实际确认 OFF。用户随后单独批准“允许，仅 sapi.sushiro.com.cn”的新 HTTPS 范围：自动捕获 1 分钟、一次正常查询、凭证只留本机；配置与执行如下，不将新授权扩大到其他主机或业务写操作。直接连接仍需 VPN 本机接管，证书锁定或校验失败时停止对应解密路径。

约 20:17–20:20 核对并保存上述唯一 sapi 范围与 1 分钟限制。约 20:20 启动手机 VPN，等待蓝色“正在开启”变为稳定绿色停止按钮后才开启 MITM 和 HTTP 捕获。西单大悦城店刷新前完整页面时间 20:15，堂食 1942 / 1945 / 1946、预约 7421 / 7410 / ---；约 20:21 只点底部刷新一次，稳定页面时间 20:21，堂食 1963 / 1964 / 1967、预约 7421 / --- / ---。返回 Surge 时三个临时开关实际 ON，随后依次关闭，数据包捕获 OFF。会话 `2026-10-02-202107` 为 5 KB / 1 个已完成 DIRECT GET，实际路径为 `https://sapi.sushiro.com.cn/gateway/wechat/api/2.0/getStoreById`；只记录已观察路径，不推测旧 stores 端点在新主机上的地址。

用户直接提供该会话 HAR，本机私有目录 0700 / 文件 0600 检查通过，不记录私有绝对路径或把原始文件放入仓库。受控核验确认恰好一条请求，20:21:25+08:00，HTTP 200，query 只有 storeId。只核对请求头名称和 authorization 的 Bearer scheme，存在 authorization、x-app-client、x-app-code，值未输出；尚未验证各头必要性、有效期或更新方式。响应是直接 Store JSON 对象，非 envelope，存在 id/name/address/wait/waitTimeCounter/waitTimeCap/groupQueuesCount，以及 groupQueues 下 reservationQueue/counterQueue/boothQueue/mixedQueue。

随后安全白名单核对确认 query/body ID 一致，店铺 3004 / 西单大悦城店，boothQueue 与 mixedQueue 均为 [1963, 1964, 1967]、reservationQueue 为 [7421]、counterQueue 为 []，对应 20:21 公共页面。groupQueuesCount=68、wait=95、waitTimeCounter=-1、waitTimeCap=180，storeStatus=OPEN、netTicketStatus=ONLINE，只记录原始值；单位未确认、-1 语义未确认，不解释为分钟、桌数或可自动取号资格。后续独立请求成功如下，必要头、来源更新时间及刷新频率仍未验收。

约 20:34–20:37 已完成固定 sapi getStoreById?storeId=3004 的独立 GET；精确请求本地时间戳未记录，不补造秒数。第一次 Python 默认 CA 发生 tls_error；指定此前系统公开 CA 后 TLS 连通，最小 authorization/x-app-client/x-app-code 与固定 Accept/Content-Type 组合为 HTTP 400。补回正常请求的 User-Agent、Referer、Content-Type 后，同一固定地址一次请求 HTTP 200、182 ms，ID/店名一致。脚本本机读取凭证、禁止重定向和环境代理、保持完整 TLS、15 秒超时与 2 MiB 上限，没有打印头值或原始正文。

独立响应原始白名单值为 wait=80、groupQueuesCount=53、waitTimeCounter=-1、waitTimeCap=180，boothQueue=['2002-2', '2007', '2010']，mixedQueue=['2002', '2007', '2010']，reservationQueue=['7421']，counterQueue=[]。与原 HAR 不同，证明是独立请求，不证明源延迟、30 秒频率或续期。保留完整号码后缀，不转换成整数、全局游标或过号桌数；单位与 -1 语义仍未知。必要头没有逐项拆分验证，本次 200 只证明该组合当时可用。

实际新 CLI 在 20:43:01 成功，319 ms，落库一条 live / miniapp_gateway / 3004，report 实际 schema 2、1 成功 0 失败。公共规范化 wait=35、groupQueuesCount=26 均 unknown 单位，booth/mixed=['2063','2064','2065']、reservation/counter=[]，源时间 unknown。随后私有辅助脚本以所有头值不得为输出子串检查，将短 x-app-client 标识与公共数字巧合判为泄漏并 exit 1；回查证实仅公共白名单，长鉴权及 User-Agent/Referer/app-code/Content-Type 值未落库。误报已修正，不是 CLI/API 失败，不公开该短标识。

20:45:16.961（UTC 12:45:16.961）使用本机私有 HAR 注入的进程环境执行 60 秒 / 2 轮 collect，首轮 HTTP 401 / http_error、exit 1，按设计立即停止；没有第 2 轮、等待、30 秒阶段或自动重试。随后 report exit 0、database_schema_version=2，西单 miniapp_gateway 共 2 条，即先前 1 成功与本次 1 失败，安全错误与来源标签正确。60/30 秒采样未验收。

401 后仅本机解析 Bearer JWT-like payload 的 exp，没有输出其他 claims/账号标识或验证签名。声明到期为 2026-10-02T12:44:37+00:00（北京时间 20:44:37），20:45:53 检查已过期，与 20:43 成功和 20:45 的 401 时序吻合；这不是签名验证或唯一因果证明。静态鉴权有效期已成为连续采集的实际阻碍，正常续期尚未捕获或实现，不能宣称所有门店全天可访问。后续只在用户已授权的本人正常查询流程核实更新，不继续重试到期值、不使用上游共享令牌、不猜 refresh 端点。

公开主源研究仍未找到 sapi 官方说明或有效公开源码引用，旧参考基址仍 crm；未找到不证明接口不存在，没有猜端点或使用共享凭证。固定 gateway profile、独立环境变量、来源隔离和迁移已实现并通过 116 项检查，新 CLI 单份快照通过，gateway 目录仍不发请求。先核实正常凭证更新，再验证有限采样、目录、另外两店、单位与源时间。

本轮并行核对的最新公开参考源码仍使用同一域名：CLI 提交仍为 `982bebfb92fb57f017f9f27878cfd467f1da0410`；overdose 最新核对提交为 `a2514e3c5044c1b9ab8516e37fd63ca2c5e99af4`，查询基址仍为该域名下 `/wechat/api/2.0`。这只是源码线索，不证明官方手机小程序当前实际请求地址；未读取、输出或使用源码附带的令牌。[CLI 查询基址](https://github.com/lmxx1234567/sushiro-cli/blob/982bebfb92fb57f017f9f27878cfd467f1da0410/internal/api/types.go#L6)、[overdose 查询基址](https://github.com/Ryujoxys/sushiro-overdose/blob/a2514e3c5044c1b9ab8516e37fd63ca2c5e99af4/internal/app/queue_live.go#L23)

本次用户主动提供的一条 HAR 已在本机检查，包含 authorization 头名称，值不输出，也不据此推断所有手机版本的导出行为或授权续期。[Surge iOS 官方发布说明](https://kb.nssurge.com/surge-knowledge-base/release-notes/surge-ios) 记录 4.13.0 起支持 HTTP/HTTPS 导出 HAR、3.6.0 起支持全部 dumped requests 导出 `.surgearchive`；当前手机版本和所有导出菜单未核验。后续仍只处理受限单会话文件，本机解析，禁止发送凭证或原始正文给模型、上传在线 HAR 网站或提交仓库。用户交付不等于授权公开内容；显示筛选不保证导出隔离，须核对实际条数和主机。仅内存元数据方案不包含导出；单次独立请求与新客户端已成功，首轮频率采样 401 后停止，正常续期及必要头核实后再恢复采样验收。

本轮临时手机 VPN、HTTP 捕获与解密已关闭；Direct、过滤规则与 CA 信任为后续单域名对照保留，尚未完全恢复。整个手机试验结束时恢复本轮修改的出站模式及捕获过滤设置，由用户取消临时手机证书信任并移除本轮新装描述文件，保留原有配置。Mac 局域网代理未启用，无需将其误记为本轮已开启后恢复。

若后续改用 Mac 作为手机代理，需另行明确同意临时开放局域网 HTTP 代理：手机 HTTP/HTTPS 流量会经过 Mac，解密和内容捕获仍只限指定寿司郎域名。开启前核对实际局域网地址、HTTP 监听端口及恢复办法，不把回环地址 127.0.0.1、SOCKS5 / HTTP API 端口或文档默认端口当作手机可用服务器事实；记下并在结束时恢复手机原 Wi-Fi 代理状态。[Surge 配置说明](https://manual.nssurge.com/profile/general.html)、[Apple Wi-Fi 设置](https://support.apple.com/guide/iphone/manage-wi-fi-settings-iphw5gjwl8k2/ios)
