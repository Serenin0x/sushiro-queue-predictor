# 实时数据接入验证手册

当前本地与已发布工具均为 `0.2.0.dev2`。已核验功能提交 [8fe3b2d2bb5bee8d3326aae0455f8a79a4142f1a](https://github.com/Serenin0x/sushiro-queue-predictor/commit/8fe3b2d2bb5bee8d3326aae0455f8a79a4142f1a)，树 b86fdc92ff270ec42af62eb9b2cb88f825a706c6，35 个远端 blob 与本地逐一一致并同步，发布实证见 [PROJECT_HANDOVER.md](PROJECT_HANDOVER.md) E0026；开放 Light 标题的先前发布见 E0024。dev2 完成显式私有上下文文件、逐 GET 整组更新、声明到期保护与来源隔离质量报告，主执行者 166 项 / 0.390 秒离线检查及独立源码复核通过，见 E0025。真实证据仍为此前西单单店成功及 60 秒采样首轮 HTTP 401 后停止；30 秒阶段未执行，官方正常更新来源、字段单位、源刷新、三店权限与连续接入仍待验收。最新 HAR 尚未收到，本轮没有新 API 查询，仍不是 1.0。

当前主线是 **本人正常查询上下文 → 固定 sapi 单店快照 → 60 秒、30 秒有界试点 → 故障与正常更新验收**。执行时明确选择 `--api-profile miniapp_gateway`；旧 crm、Mac 及手机空捕获是历史诊断，不是重新接入的必经步骤。西单 3004 已核验，另两店可由本人正常详情 GET 的 query/body ID 与店名核对，无需等待未知的新目录端点。两轮成功仅证明该短窗可查询，不代表全天稳定或 1.0 已验收。2026-10-03 夜间用户手动刷新后报告新请求和三个开关已关闭，又补充可能多次刷新；最新 HAR 尚未交付，无本轮新凭证解析或新 API 查询结果。主执行者此前约 121 秒越时且未点到微信刷新的事实完整保留 E0024。

## 已取得的证据

| 项目 | 2026-10-02 的实际结果 |
| --- | --- |
| 旧 legacy 目录地址 | `https://crm-cn-prd.sushiro.com.cn/wechat/api/2.0/stores`；历史线索，不是新 gateway 目录 |
| 旧目录协议线索 | GET，查询参数 latitude、longitude、numresults；来自锁定的大陆开源客户端源码 |
| 旧目录匿名真实测试 | 系统网络工具正常校验证书后 HTTP 401；新客户端使用有效的系统 CA 文件也得到 401 |
| 本机 Python 初次失败 | 默认 CA 环境缺失，证书验证 code 20；使用现有系统公开 CA 文件后连通。没有关闭 TLS 验证 |
| 旧单店详情线索 | legacy GET `/wechat/api/2.0/getStoreById?storeId=...` 来自参考源码；该旧主机的真实详情未验收，不能与已观察的新 gateway 路径混淆 |
| 来源字段语义与刷新周期 | 尚未验证；旧目录 401 证明该地址回应并要求鉴权，新详情 200 证明当时可读，均不证明字段单位、鉴权续期或 30 秒实时能力 |
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

纯本机零网络 auth-status 已实现，并通过此前 dev1 真实配置的安全本机核验；当前 dev2 全套 166 项离线检查通过。按 --api-profile 只读对应授权环境变量（legacy 使用 SUSHIWAIT_QUERY_AUTHORIZATION，miniapp_gateway 使用 SUSHIWAIT_GATEWAY_AUTHORIZATION），不读取其他上下文头/CA，不构造网络客户端/opener/数据库，不联网或落盘。只选取和解释 iat/exp，不输出或保存其他 claims/token。已验证运行方法：

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

当前路线明确使用 `--api-profile miniapp_gateway`。命令仍默认 `legacy`，省略选项不会自动切到已验证的新主机；没有自动探测、跨主机回退或任意 URL 选项：

| Profile | 固定目的地址 | 当前允许只读端点 |
| --- | --- | --- |
| miniapp_gateway | `https://sapi.sushiro.com.cn/gateway/wechat/api/2.0` | 仅已观察的 getStoreById；stores 返回 unsupported_endpoint，零网络请求 |
| legacy | `https://crm-cn-prd.sushiro.com.cn/wechat/api/2.0` | stores、getStoreById；旧目录匿名测试为 401，作为历史诊断能力保留 |

新 gateway 的目录路径没有证据，不能将旧 stores 路径加到新基址。核实试点 ID 可直接使用用户正常详情请求：确认 query 的 storeId、响应 id 与店名对应，再保存该店的核验范围；不猜数字 ID 或用合成 ID 访问真实服务。目录仍是独立待发现能力，不阻塞已核实 ID 的单店或 1–3 店试点。

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

### 显式私有查询上下文文件（dev2）

`--credentials-file` **仅由 snapshot 和 collect 支持**，与 --anonymous 互斥。不传文件时仍用上表相互隔离的环境变量；该模式缓存本次进程初始查询上下文，修改环境需重启。auth-status 与 stores 仍只读环境变量、不接受文件参数；auth-status 的结果不会自动对应到另一个文件。文件模式不从环境补授权或五项上下文头，只有可信 CA 的 `SUSHIWAIT_CA_FILE` 继续由本机环境配置。

文件由用户本人已授权的正常查询流程在本机受控生成。工具只读取显式指定文件，不寻找登录态、不导入 HAR、不猜 refresh 端点。不要将原始 HAR 当作这个 JSON 文件，不把任何头值粘贴到聊天、命令参数或仓库。以下只描述字段契约，不提供令牌示例：

| JSON 键 | 严格契约 |
| --- | --- |
| schema_version | 整数 1，bool 不接受 |
| api_profile | 与命令一致的 legacy 或 miniapp_gateway |
| revision | 整数 1–9223372036854775807，bool 不接受；更换整组上下文时递增 |
| authorization | 合法、非空 ASCII 字符串，裸查询值或完整 Bearer 值；输入及规范化后的 Bearer 字符串均不得超过 8 KiB（8192 字节），裸值须给前缀留空间 |
| app_client、app_code | gateway 必须包含这两个键；值为合法字符串或 null，字符串最多 1024 字符且无空格/控制字符 |
| user_agent | gateway 必须包含；合法字符串或 null，最多 2048 字符，允许普通空格 |
| referer | gateway 必须包含；合法字符串或 null，最多 2048 字符且无空格/控制字符 |
| content_type | gateway 必须包含；合法字符串或 null，最多 256 字符，允许普通空格 |

legacy 文件必须恰好含前四个键；gateway 必须恰好含全部九个键。gateway 的 null 明确表示本次整组不发送该头，不从环境继承旧值；真实服务是否接受缺某头仍待验证。所有头字符串仅接受可打印 ASCII、不得为空或只含空格。拒绝额外或重复 JSON 键、非有限数字、非法 UTF-8/JSON、profile 不符和不合法值；总文件上限 **16 KiB（16384 字节）**。

文件读取仅支持具备 `O_NOFOLLOW` 等安全能力的 POSIX 系统。路径全部祖先目录均不得为符号链接；**直接父目录和文件**须属当前有效用户。直接父目录权限只接受 0700 或 0500；文件必须是常规文件、仅一个硬链接，权限只接受 0600 或 0400。读取前后检查身份、大小与时间信息，检测到读取中修改/替换就停止，不使用部分内容；不安全路径、权限、所有者、类型、大小或 JSON 都产生安全分类，不输出文件路径、原始内容或解析异常正文。当前不支持 Windows 文件模式，也不会自动放宽权限。

可在仓库外准备私有目录，并只把文件路径传给命令。以下不创建令牌文件；应先确认整条路径没有符号链接，再由本机受控来源创建符合上表的完整文件：

```sh
PRIVATE_QUERY_DIR="$HOME/.config/sushiwait/query"
mkdir -p "$PRIVATE_QUERY_DIR"
chmod 700 "$PRIVATE_QUERY_DIR"
export SUSHIWAIT_CONTEXT_FILE="$PRIVATE_QUERY_DIR/gateway.json"
```

整组更新必须使用**同一私有目录内的原子替换**：先将全部九个键、同一份正常请求上下文和递增 revision 写入一个新的常规文件，校验完整后设 0600，再在同目录 rename 替换目标；不要逐键改写正在使用的文件。若本机受控流程已经准备好 `gateway.next.json`，替换步骤为：

```sh
chmod 600 "$PRIVATE_QUERY_DIR/gateway.next.json"
mv -f "$PRIVATE_QUERY_DIR/gateway.next.json" "$SUSHIWAIT_CONTEXT_FILE"
```

两文件在同目录、同文件系统时该 rename 为原子替换；目录 0500 或文件 0400 可用于只读输入，但准备更新需要合法的目录写权限。工具不负责取得新上下文或写这个文件。revision 检查仅在当前进程：禁止倒退，同 revision 内容不同为冲突，同 revision 内容相同可继续；重启后不保留上一进程的最大 revision。revision 递增只说明本机配置变化，不证明官方完成续期。

每个 GET 前重新读取整组文件，包括同轮的不同门店；上下文有变化时先完整构建新的客户端再替换，每个 GET 只用一组。等待中最多每 60 秒重读，并按声明到期前的保护期限提前唤醒。由此可以接收本机正常更新，但官方更新来源与自动续期仍未知。

snapshot/collect 请求前检查声明 iat/exp，未验证 JWT 签名；声明无效返回 auth_claims_invalid，已到期为 auth_declared_expired，剩余不超过 30 秒为 auth_expiring。**这些都是零网络的本机停采**，不是上游 401。等待到了保护期限仍无有效更新就停止，文件读取或配置错误也停止整次采样。opaque、缺失 exp 等未知到期不填默认 TTL，也不表示服务器会接受；真实 GET 失败仍首错停止、没有自动重试。已有在途 GET 不会因为文件替换而混入新头，下一次 GET 再检查。

新 profile 和此前实际 dev1 CLI 单店保存已核验。符合契约的私有文件与可信 CA 就绪后，西单已验证 ID 的单次命令为：

```sh
PYTHONPATH=src python3 -m sushiwait snapshot --api-profile miniapp_gateway --credentials-file "$SUSHIWAIT_CONTEXT_FILE" --store-id 3004 --db data/local/gateway-pilot.sqlite3
PYTHONPATH=src python3 -m sushiwait report --db data/local/gateway-pilot.sqlite3
```

2026-10-02 实际尝试 60 秒 / 2 轮采样，首轮在 20:45:16.961 得到 HTTP 401 后立即停止，没有第 2 轮或周期等待；30 秒阶段未执行，60/30 秒采样均未验收。report 成功读取同店同 profile 的 1 成功与 1 失败。到期静态凭证不再重试；从本人正常查询流程取得新的本机上下文，检查声明时间与单次实际可用性后，才进入下列有界验证。先执行 60 秒；仅在其成功、无异常且调用范围允许时再执行 30 秒。两条都是后续执行示例，不是已完成记录：

```sh
PYTHONPATH=src python3 -m sushiwait collect --api-profile miniapp_gateway --credentials-file "$SUSHIWAIT_CONTEXT_FILE" --store-id 3004 --interval 60 --samples 2 --db data/local/gateway-pilot.sqlite3
PYTHONPATH=src python3 -m sushiwait collect --api-profile miniapp_gateway --credentials-file "$SUSHIWAIT_CONTEXT_FILE" --store-id 3004 --interval 30 --samples 2 --db data/local/gateway-pilot.sqlite3
```

上述文件命令只有在本机已经准备好完整私有文件后才能执行；省略 --credentials-file 可继续使用对应 profile 的环境模式，但更新环境需重启。dev2 的重读/到期保护已离线验证，没有新的真实采样验收；文件更新来源与实际有效边界须另行记录，不靠连续重启或失效值重试代替正常更新验证。Ctrl-C 返回 interrupted / exit 130；修正本机问题或取得正常更新后可用同一 --db 继续追加历史，每次运行有新 run_id、轮数重新开始，**不能恢复上次未完成任务的采样游标/剩余轮数**。

### 旧 legacy 诊断与命令（历史保留）

此前 Mac 调试只获 `crm-cn-prd.sushiro.com.cn` 范围确认。用户安装 Mac CA 后界面实际显示系统已信任，这与 Python 默认 CA 缺失不同；最多 3 分钟 / 50 MB / 100 请求的旧捕获窗口中，正常刷新与完整重开均未见小程序请求。匿名浏览器页面曾被工具阻止，但请求出现在 Surge；匿名 curl 经本机代理正常 TLS 得到 401。Mac 阶段没有取得正常小程序凭证，之后 sapi 手机成功是另一个阶段，不能覆盖旧失败。

Mac 历史结束时临时解密与 HTTP 捕获关闭，旧单域名和过滤规则保留，用户安装的证书信任由用户管理；恢复“自动额外 MITM”曾被自动审批拒绝，理由是会扩大其他主机范围，因此保留 OFF，未绕过拒绝。局域网代理未开启。后续需要调试时按当前已授权范围另做实际核对，不重放旧 crm 流程或遇证书锁定继续解密。

这些命令针对旧 crm，不是当前 sapi 主线。旧目录此前单次匿名连通结果为 401，不能据此取得三店 ID 或证明新 gateway 目录存在：

```sh
PYTHONPATH=src python3 -m sushiwait stores --api-profile legacy --anonymous --match 中关村 --match 西单 --match 世豪
```

若另有对应 legacy 的有效授权与核实 ID，可使用其独立命令；不能把 gateway ID/上下文自动视为 legacy 可用：

```sh
PYTHONPATH=src python3 -m sushiwait snapshot --api-profile legacy --store-id VERIFIED_ID --db data/local/pilot.sqlite3
PYTHONPATH=src python3 -m sushiwait collect --api-profile legacy --store-id VERIFIED_ID --interval 60 --samples 3 --db data/local/pilot.sqlite3
PYTHONPATH=src python3 -m sushiwait report --db data/local/pilot.sqlite3
```

`VERIFIED_ID` 是占位符，必须替换成对应 profile 已核实的真实数字 ID。两 profile 的 collect 均限 1–3 店、30–3600 秒周期、每店 1–120 轮；按每轮开始时间安排，若查询耗时超过周期则实际周期变长。请求失败即停止整次采样，不自动重试鉴权、限流或未知结果。30 秒可以配置，但当前没有验证上游允许或实际 30 秒更新。

工具只允许两个固定 profile 的已知只读 GET，gateway 仅详情、目录禁止；无登录、个人号单、取号、预约、取消或重排接口。不跟随重定向或读取环境代理，15 秒 I/O 超时、2 MiB 响应上限、失败不自动重试。没有守护进程、多实例锁、通知或全天调度，本机不要同时运行多个采集进程。

## 电脑试点批量与后期同步展示

用户要求在电脑快捷批量测试门店，取得有效本人查询授权并核实目标 ID 后有节奏执行，不需要逐店手机抓包。当前 collect 可重复传 --store-id，但最多 1–3 店、每店 1–120 轮，首个失败停止；这是试点批量，全国目录和全国批量仍待实现/验收。同一 profile 的多店权限也须核实，不能从西单一次成功推断所有门店均可访问。以下两个 ID 都是占位符，必须先换成对应 profile 已核实的真实 ID；当前另外两家试点仍为 null，本轮没有执行这个多店示例：

```sh
PYTHONPATH=src python3 -m sushiwait collect --api-profile miniapp_gateway --credentials-file "$SUSHIWAIT_CONTEXT_FILE" --store-id VERIFIED_ID_A --store-id VERIFIED_ID_B --interval 60 --samples 1 --db data/local/gateway-pilot.sqlite3
```

后期本项目小程序需要同步显示/刷新所选门店当前堂食与预约叫号，复用服务端共享采集，让用户减少在两个小程序之间来回切换。仍按有限展示集合保存与显示，分清请求接收时间和未知的源更新时间，明确刷新失败/过期状态，不把旧数据或失败当作无人排队。此项属于 R23，须等 R18 的真实字段、刷新与连续接入齐备后才实现前端；当前没有服务端持续调度或小程序展示功能。

## 输出与本地存储

命令输出 JSON；数据库仅保存规范化公共观察与安全失败分类，文件在 `data/local/`，默认 `sushiwait.sqlite3`。该目录被 Git 排除。posix 环境新目录 0700、数据库 0600；拒绝符号链接数据库，未知数据库版本不覆盖。

- 规范化快照 `schema_version=1` 保持；它与数据库和 report 的版本独立。SQLite `PRAGMA user_version=2`，样本保存 run_id、store_id、data_origin、api_profile、received_at、ok 与规范化 JSON；当前 dev2 全套 166 项离线检查通过；此前实际 dev1 CLI 使用 schema 2 数据库成功保存。本轮没有新增数据库迁移。
- 观察、目录项与变化输出顶层 api_profile，仅允许 legacy / miniapp_gateway，不保存任意来源 URL；端点固定映射由客户端负责。compute_change 只有同店、同真实/合成来源、同 API profile 才比较，报告也按三者分组。
- 写打开旧 v1 数据库在显式事务中为旧成功/失败行补 legacy 列与 JSON 标签，保留行 ID、run_id 与内容；冲突 profile 或坏 JSON 拒绝并回滚。只读打开 v1 不迁移，使用虚拟 legacy 视图；report 的 schema_version=2 表示输出格式，database_schema_version 单独反映磁盘库版本。
- normalized 字段分别记录 present / missing / null / invalid。缺失不会变成 0，缺失队列不会变成空数组。
- 保留四类 groupQueues 的完整字符串数组、顺序与重复情况，不将展示最大号码当作全局游标。
- `raw_wait` 与 `groupQueuesCount` 单位均为 unknown；未经小程序/现场对照，不解释为分钟、人数或已签到桌数。waitTimeCounter / waitTimeCap 另按有符号整数保存 presence/value/unit=unknown，拒绝 bool，保留 -1 等原值；不猜测 -1 的禁用含义或正值的分钟单位。
- waitTimeCounter / waitTimeCap 的缺失、null、invalid 分别记录，公共哈希与标量变化覆盖两字段；旧快照缺新键按 missing/None 比较，不当 0、不修改历史 JSON。有符号等待字段与本机 auth-status 的既有回归保持；当前 dev2 全套 166 项通过，快照 schema_version 仍为 1。
- `request_started_at`、`received_at`、`elapsed_ms` 是本地请求时间；`source_updated_at=null`，`upstream_freshness=unknown`。
- content_hash 只作用于规范化公共字段；相同 hash 仅表示所保存字段相同，不能证明缓存或源新鲜度。
- 未知字段记录安全键名清单；未知对象、个人字段值和原始响应不落库。如果以后发现有用字段，再核实语义、权限并加入白名单。
- 失败不生成成功快照；GET 返回错误为 failure_phase=request，收到正常响应但规范化失败为 normalization。本机文件/声明/配置保护为 preflight，只保存 checked_at、http_status=null 和安全鉴权时间元数据，不虚构请求开始/响应接收/耗时；三者都使 collect 首错停止。数据库沿用 received_at 列保存事件时间，preflight 行在此列保存 checked_at。

### 只读质量报告（dev2）

运行 report 不联网，也不取得或更新凭证；只读旧 v1 不迁移文件。报告格式 `schema_version=2`、实际库版本 `database_schema_version` 保持独立。数据库 schema/列结构不支持、非法 profile 或物理损坏仍可拒绝整库；坏 JSON 容错不是任意数据库恢复承诺。

每个 `store_id + data_origin + api_profile` 分组的 `samples / successful_samples / failed_samples` 和时间范围来自全历史；其中 `successful_samples` 仅按数据库 ok 标记计数，不能代替有效载荷核验。新 `first_recorded_at / last_recorded_at` 表示记录时间，兼容的 `first_received_at / last_received_at` 值相同；`record_time_semantics=response_received_or_preflight_checked` 明示它们可能包含本机预检 checked_at，不能全部解释为 HTTP 响应接收时间。

`quality` 单独分析**每组最新最多 10000 个 sample ID**，不是某段时间或全库统一窗口。CLI 使用默认上限；window 显示 selection=latest_sample_ids、sample_limit、included_samples、total_samples、truncated、first/last_sample_id 和 payload_size_limit_bytes=2097152。先选最新 ID、再按 ID 升序逐行统计，超过 2 MiB 的单条 payload 在 SQL 读取阶段替换为 null，不将完整超大正文载入 Python；坏 JSON、重复键、NaN/Infinity 字面量、非对象和解析错误计入 invalid_json_or_oversized_payloads。

| quality 内容 | 正确理解 |
| --- | --- |
| failures.by_phase / error_codes_by_phase | request、normalization、preflight、unknown 四桶；白名单之外错误码为 unknown。旧失败无 phase 默认 request，未知 phase 为 unknown；不根据错误码猜阶段 |
| failures.local_stops / actual_request_failures | local_stops 是 preflight；actual_request_failures 是 request+normalization 的记录数，不能直接称 HTTP 失败率，normalization 可能是 HTTP 200、request 也含客户端提前拒绝 |
| failures.http_status_by_phase | 只统计 request/normalization **失败记录**；合法 100–599 整数为字符串数字，null 为 none，其余为 unknown。不统计成功、preflight、unknown；旧成功记录没有 HTTP status，不能虚构成功状态分布 |
| requests.records / elapsed_ms | 可解析成功记录及 request/normalization 失败的请求元数据；带时区起止、接收不早于开始且耗时为合法非 bool 非负整数时才计入耗时。无效增加 invalid_timing_records |
| requests.start_interval_ms | 同 run 的相邻有效请求开始间隔，可包含 request/normalization 失败；preflight/unknown、坏 JSON、未知记录类型、无效 timing 断链，不跨 run。倒退对计 reversed_start_pairs，不计该对间隔；当前记录仍可作为后续起点 |
| public_content | 有效/无效成功载荷、相邻可比较对数、内容变化/未变与标量变化次数；仅同组同 run 相邻有效成功比较，任意失败、坏/超大 JSON、未知类型或无效成功快照断链。无效 timing 单独不一定中断公共内容链 |
| field_presence | 公共标量与 groupQueues 的 missing/null/invalid/present/unknown 次数；陌生 presence 为 unknown、错误 present 值为 invalid，坏值或私人元数据不回显 |
| queues | reservationQueue/counterQueue/boothQueue/mixedQueue 的 presence、comparable_pairs、display_set_changes、display_values_changes；两端都为合法 present 数组才比较。set 忽略顺序/重复，values 保留顺序/重复；报告不输出具体号码 |

elapsed_ms 与 start_interval_ms 汇总为 count/min/max/mean，mean 四舍五入到三位小数；没有可用样本时 min/max/mean 为 null。requests 的窗口首末请求开始时间只来自有效本机时间。坏成功计 invalid_successful_payloads，坏失败归 unknown；这些统计与正常成功字段 presence 分开。

报告始终 `upstream_freshness=unknown`，不计算 ETA、真实过号率、全局游标或源刷新周期。相同公共内容不证明缓存，不同内容不证明每个周期源更新；请求间隔是本机采集节奏，不能替代页面/现场和上游时间对照。report 不回显停止记录的 auth_status，安全声明时间仅在本机停止输出/记录中保留。

## 当前执行路线与实际验收

1. **核实本人正常查询上下文。** 使用用户已有授权的正常访问流程，目的地址限 `sapi.sushiro.com.cn` 已观察的详情 GET，保持完整 TLS。查询上下文仅在本机受控位置/进程内使用；文件模式准备完整私有 JSON 并由 snapshot/collect 做请求前声明检查，环境模式可先用 auth-status 记录对应环境中的声明。auth-status 不读 --credentials-file，两者输入不可混为同一份。声明未验签、不证明服务端接受或续期，exit 0 不能作为可用凭证判定；正常更新来源尚未知，不能猜 refresh、发旧主机凭证或使用共享 token。
2. **核实门店 ID。** 西单 3004 已核实；中关村、成都从本人正常进入该店产生的详情请求中核对 query/body ID 与店名，记录单店范围后回填配置。有效授权和 ID 核实后，可在电脑对这 1–3 店有节奏查询，不必逐店重复手机抓包。新 gateway 目录未知，不是当前单店验证的前置条件；相同 ID 在不同 profile 间不自动互认。
3. **先保存一份真实快照。** 用上文 miniapp_gateway snapshot/report 命令验证当前上下文、目标店身份、安全公共字段及失败记录。对照小程序时明确点底部刷新并记录前后展示集合与页面时间；截图只解释页面，不能取得授权或请求签名。源更新时间与单位未知就保留未知，区分堂食/预约各组展示，完整号码及后缀不转成全局游标或过号数。
4. **先 60 秒，再有条件做 30 秒。** 单次当前查询成功后执行 60 秒 / 2 轮；确认无鉴权、TLS、限流或其他异常，且调用范围允许，才执行 30 秒 / 2 轮。保存本地请求起止、耗时、公共变化及安全失败；使用文件模式时依规则整组原子更新，并核对 revision 与实际正常更新证据。本机到期/坏文件停采和真实请求失败分开记录，均停止整次任务；不重试、不把缓存或旧值当新数据。两轮的成功范围只限该短窗；30 秒请求间隔不是上游允许频率或更新周期的证据。
5. **在营业和有变化的场景做对照。** 2026-10-03 01:15 镜像核对西单页最后更新时间 01:13，堂食/预约均 `---`，用户报告凌晨停业。停业空值可用于记录页面状态，不能证明无人排队、叫号推进、单位或数据新鲜度；当时没有新 API 请求。后续主执行者捕获约 121 秒越时且未点到微信刷新，01:36 独立核验临时开关关闭；用户随后手动操作并报告新请求、三项关闭及可能多次刷新，最新 HAR 仍待交付，不能验收实际时长/次数或源频率。完整事实保留 E0024。页面时间与本地接收时间分别记录，不能把页面时间虚构为 API 源更新时间。
6. **逐项验收核心稳定性。** 对每份新凭证记录实际成功/失败边界及本人正常更新证据；覆盖三店、营业期变化、缺失字段、到期/限流/TLS/网络失败与停机结果，再扩大采样。dev2 已离线验证整组文件重读、声明保护和质量报告；用 report 区分全历史与有限窗口、preflight 与请求/规范化失败、实际间隔和公共变化。gateway 仍没有目录，官方正常更新来源、全天调度和连续真实采样未验收；环境更新需重启、重启同库只是续写历史，未恢复任务游标。两份快照或两轮 collect 不能替代持续可靠性验收。记录未完成项后再决定后续工具与版本，不以用户的 1.0 目标宣称已稳定。

实际操作的凭证与原始网络记录留在本机受控位置，公开仓库只记录无秘密的结构、证据与结论。这次用户授权使用自己的账号协助接入；它不包含真实取号、取消或重排。

## 2026-10-02 手机调试历史与保留设置

以下按时间保留此前试验，不能把早期“没有请求/凭证”或 crm 范围当成当前结论。用户批准新 sapi 后，正常 GET、HAR 白名单页面对应、独立 HTTP 200 与 dev1 新 CLI 单份保存均完成；固定 profile、来源隔离和 116 项检查通过。60 秒采样首轮 401 后停止，30 秒阶段未执行，静态鉴权声明到期与时序吻合，正常续期、必要头、单位和源刷新频率未验收。2026-10-02 试验结束时实际核验手机 VPN、MITM、HTTP 捕获与数据包捕获均 OFF；Default 直接连接、sapi 规则、CA 信任及服务端验证保留，自动额外 MITM OFF。该历史核验不代替下一轮启动前的实际检查。凭证与原始 HAR 留私有位置，不进公开仓库或模型，Mac 局域网代理未启用。[Surge 原理说明](https://manual.nssurge.com/book/understanding-surge/en/)、[MITM 核验说明](https://kb.nssurge.com/surge-knowledge-base/faq/common-faqs)

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

公开主源研究仍未找到 sapi 官方说明或有效公开源码引用，旧参考基址仍 crm；未找到不证明接口不存在，没有猜端点或使用共享凭证。固定 gateway profile、独立环境变量、来源隔离和迁移已实现并通过 116 项检查，新 CLI 单份快照通过，gateway 目录仍不发请求。当前按上文正常本人上下文、单份快照与有界试点推进，另外两店 ID 可从正常详情核实；单位、源时间和正常更新继续验收，目录另行发现，不作为详情试点前置条件。

本轮并行核对的最新公开参考源码仍使用同一域名：CLI 提交仍为 `982bebfb92fb57f017f9f27878cfd467f1da0410`；overdose 最新核对提交为 `a2514e3c5044c1b9ab8516e37fd63ca2c5e99af4`，查询基址仍为该域名下 `/wechat/api/2.0`。这只是源码线索，不证明官方手机小程序当前实际请求地址；未读取、输出或使用源码附带的令牌。[CLI 查询基址](https://github.com/lmxx1234567/sushiro-cli/blob/982bebfb92fb57f017f9f27878cfd467f1da0410/internal/api/types.go#L6)、[overdose 查询基址](https://github.com/Ryujoxys/sushiro-overdose/blob/a2514e3c5044c1b9ab8516e37fd63ca2c5e99af4/internal/app/queue_live.go#L23)

本次用户主动提供的一条 HAR 已在本机检查，包含 authorization 头名称，值不输出，也不据此推断所有手机版本的导出行为或授权续期。[Surge iOS 官方发布说明](https://kb.nssurge.com/surge-knowledge-base/release-notes/surge-ios) 记录 4.13.0 起支持 HTTP/HTTPS 导出 HAR、3.6.0 起支持全部 dumped requests 导出 `.surgearchive`；当前手机版本和所有导出菜单未核验。后续仍只处理受限单会话文件，本机解析，禁止发送凭证或原始正文给模型、上传在线 HAR 网站或提交仓库。用户交付不等于授权公开内容；显示筛选不保证导出隔离，须核对实际条数和主机。仅内存元数据方案不包含导出；单次独立请求与新客户端已成功，首轮频率采样 401 后停止，正常续期及必要头核实后再恢复采样验收。

本轮临时手机 VPN、HTTP 捕获与解密已关闭；Direct、过滤规则与 CA 信任为后续单域名对照保留，尚未完全恢复。整个手机试验结束时恢复本轮修改的出站模式及捕获过滤设置，由用户取消临时手机证书信任并移除本轮新装描述文件，保留原有配置。Mac 局域网代理未启用，无需将其误记为本轮已开启后恢复。

若后续改用 Mac 作为手机代理，需另行明确同意临时开放局域网 HTTP 代理：手机 HTTP/HTTPS 流量会经过 Mac，解密和内容捕获仍只限指定寿司郎域名。开启前核对实际局域网地址、HTTP 监听端口及恢复办法，不把回环地址 127.0.0.1、SOCKS5 / HTTP API 端口或文档默认端口当作手机可用服务器事实；记下并在结束时恢复手机原 Wi-Fi 代理状态。[Surge 配置说明](https://manual.nssurge.com/profile/general.html)、[Apple Wi-Fi 设置](https://support.apple.com/guide/iphone/manage-wi-fi-settings-iphw5gjwl8k2/ios)
