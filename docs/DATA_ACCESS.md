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
| Mac 官方小程序 | 用户已正常刷新并完整关闭后重开，数字正常；没有观察到访问候选域名的查询，自动化读取小程序窗口仍超时 |
| Mac 临时请求调试 | 用户已批准仅 crm-cn-prd.sushiro.com.cn；Mac CA 已由用户安装且系统已信任。匿名 Chrome / curl 对照出现在 Surge，仍是 401；没有用户鉴权请求。临时解密与捕获已关闭，单域名规则保留、自动额外 MITM 保持关闭，未开启局域网代理 |
| iPhone 第一轮空捕获 | 单域名短窗已结束；约 19:45 实际看到中关村大融城店公共页面，更新时间 19:44，但保存会话为 0 B / 0 个请求。约 19:47 确认 VPN、MITM、HTTP 捕获与数据包捕获 OFF；Direct、单域名规则、CA 信任保留，自动额外 MITM OFF。没有导出、凭证或真实接口数据，空结果原因未知 |

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
3. 本次已取得仅 `crm-cn-prd.sushiro.com.cn` 临时调试的用户确认。最初 Mac Surge 没有 CA，用户安装后界面明确显示系统已信任；这与 Python 默认 CA 缺失是两个独立问题。每轮核对唯一 MITM 主机名、捕获 URL 模式 `https://crm-cn-prd.sushiro.com.cn/*`，关闭捕获自动额外开启 MITM，使用最多 3 分钟 / 50 MB / 100 请求的临时捕获。第一轮空结果后曾恢复初始设置；第二轮用户正常刷新、第三轮完整重开小程序后仍未看到小程序请求。匿名浏览器对照的页面被浏览器工具阻止，但其网络请求出现在 Surge；经本机代理的匿名 curl 正常 TLS 得到 401，也出现在列表。捕获路径可见不等于正常小程序接入已打通。Mac 当前临时解密与 HTTP 捕获均关闭，唯一主机和捕获规则保留供后续核对。恢复“自动额外 MITM”被自动审批拒绝，原因是扩大其他主机范围，因此保持关闭，未绕过拒绝。凭证未取得，用户安装的 Mac 证书保留，系统信任由用户管理；遇证书锁定停止。
4. 手机第一轮已独立看到公共门店页面，捕获会话为 0 个请求，VPN、MITM 与 HTTP 捕获已关闭；下一步仅准备同域名手机匿名 GET 对照核对捕获过滤管线，尚未执行，详情见下一节。页面正常、开关开启或证书核验均不能替代实际接入验收。
5. 目录匹配三家店真实 ID，再同步小程序数字与单店响应；记录观测时间、字段值、页面状态、偏差与变化时间。至少区分堂食、预约和签到桌数。
6. 先验证 60 秒，再在允许频率内验证 30 秒；观察鉴权过期、缓存与异常，保存缺失/失败样本，不仅保存成功样本。

实际操作的凭证与原始网络记录留在本机受控位置，公开仓库只记录无秘密的结构、证据与结论。这次用户授权使用自己的账号协助接入；它不包含真实取号、取消或重排。

## 手机端验证路线（第一轮空捕获已结束，匿名对照待执行）

截至 2026-10-02 约 19:47（Asia/Shanghai），第一轮手机单域名捕获已结束，正常公共页面可见，但保存会话为 0 B / 0 个请求；没有导出、凭证或真实接口数据，原因未知。手机 VPN、MITM、HTTP 捕获与数据包捕获均 OFF，Default 直接连接、唯一 MITM 主机及过滤规则、受信任 CA 保留，自动额外 MITM OFF，未全部恢复成试验前设置。下一轮手机匿名 GET 对照仅准备，尚未执行或再次开启；不需要现有 VLESS 节点，Mac 局域网代理未启用。[Surge 原理说明](https://manual.nssurge.com/book/understanding-surge/en/)、[MITM 核验说明](https://kb.nssurge.com/surge-knowledge-base/faq/common-faqs)

已通过手机实际页面截图核对并保存的设置：

- MITM 主机：唯一 `crm-cn-prd.sushiro.com.cn`。
- 请求记录的内存捕获过滤器：“包含关键词”，唯一关键词 `crm-cn-prd.sushiro.com.cn`。
- 磁盘 HTTP 过滤器：“通配符匹配”，唯一启用表达式 `https://crm-cn-prd.sushiro.com.cn/*`，仅 HTTP/HTTPS 请求开启。
- 自动额外 MITM：原本 OFF，保持 OFF；原有限制器 3 分钟 / 50 MB / 100 请求未变，其他原有过滤项未改。
- 跳过服务端证书校验：OFF；MITM HTTP2：OFF；自动屏蔽 QUIC：原有 ON 保持。
- 临时运行开关：手机本机 VPN、MitM、HTTP 捕获、数据包捕获均 OFF。没有取得正常小程序网络请求、查询凭证、门店 ID 或真实接口快照。

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

下一步准备在同一域名和有界范围做一次手机匿名 GET 对照，定位捕获过滤管线；尚未执行或再次开启。直接连接仍需要 VPN 本机接管。若正常请求不可见、证书锁定或校验失败，记录实际结果并停止该路径，不能以证书就绪、页面正常、捕获开启或等待时间替代接入验收。

约 19:51 用户要求先自行开启并刷新几次。已返回 Surge 捕获首页交接，告知开启顶部“启动”（保持直接连接）、MitM 与“捕获流量”，数据包捕获保持关闭，唯一主机不改；在 3 分钟窗口内重开小程序并刷新，再检查该轮记录。若仍为空，保持同一范围，用 Safari 打开 `https://crm-cn-prd.sushiro.com.cn/wechat/api/2.0/stores` 做一次无凭证 GET 对照，记录是否出现目标请求。用户接手后的开关和结果尚未核验，不将此前 OFF 状态当作之后仍 OFF。对照出现 HTTP 401 也有诊断价值，但不代表有效授权或门店数据。

官方排障资料指出捕获过滤器可能隐藏请求，但尚未找到 iOS 磁盘通配符究竟匹配完整 URL 还是主机名的明确规范。因此已保存的 `https://crm-cn-prd.sushiro.com.cn/*` 只证明界面配置值，手机匹配行为还需对照，不能写成已验证正确。[Surge 排障说明](https://kb.nssurge.com/surge-knowledge-base/guidelines/troubleshooting)

本轮并行核对的最新公开参考源码仍使用同一域名：CLI 提交仍为 `982bebfb92fb57f017f9f27878cfd467f1da0410`；overdose 最新核对提交为 `a2514e3c5044c1b9ab8516e37fd63ca2c5e99af4`，查询基址仍为该域名下 `/wechat/api/2.0`。这只是源码线索，不证明官方手机小程序当前实际请求地址；未读取、输出或使用源码附带的令牌。[CLI 查询基址](https://github.com/lmxx1234567/sushiro-cli/blob/982bebfb92fb57f017f9f27878cfd467f1da0410/internal/api/types.go#L6)、[overdose 查询基址](https://github.com/Ryujoxys/sushiro-overdose/blob/a2514e3c5044c1b9ab8516e37fd63ca2c5e99af4/internal/app/queue_live.go#L23)

后续导出只限已核对的捕获窗口，并留在本机受控位置供本机程序解析；禁止上传聊天或在线 HAR 网站。[Surge iOS 官方发布说明](https://kb.nssurge.com/surge-knowledge-base/release-notes/surge-ios) 记录 4.13.0 起支持 HTTP/HTTPS 请求导出 HAR、3.6.0 起支持将全部 dumped requests 导出 `.surgearchive` 供 Mac Dashboard 打开；当前手机版本、导出菜单、实际格式与是否保留 Authorization 尚未核验，第一轮没有已核实正常请求，尚未导出。请求列表的域名显示筛选不能保证导出只含该域名，必须先检查实际捕获范围与导出行为。取得正常查询、核对凭证来源与有效期后，才开展真实目录、三家门店 ID、字段和 60/30 秒采样。

本轮临时手机 VPN、HTTP 捕获与解密已关闭；Direct、过滤规则与 CA 信任为后续单域名对照保留，尚未完全恢复。整个手机试验结束时恢复本轮修改的出站模式及捕获过滤设置，由用户取消临时手机证书信任并移除本轮新装描述文件，保留原有配置。Mac 局域网代理未启用，无需将其误记为本轮已开启后恢复。

若后续改用 Mac 作为手机代理，需另行明确同意临时开放局域网 HTTP 代理：手机 HTTP/HTTPS 流量会经过 Mac，解密和内容捕获仍只限指定寿司郎域名。开启前核对实际局域网地址、HTTP 监听端口及恢复办法，不把回环地址 127.0.0.1、SOCKS5 / HTTP API 端口或文档默认端口当作手机可用服务器事实；记下并在结束时恢复手机原 Wi-Fi 代理状态。[Surge 配置说明](https://manual.nssurge.com/profile/general.html)、[Apple Wi-Fi 设置](https://support.apple.com/guide/iphone/manage-wi-fi-settings-iphw5gjwl8k2/ios)
