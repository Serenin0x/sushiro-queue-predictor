# 实时数据接入验证手册

当前本地为`0.2.0.dev4`、226项/0.466秒检查通过、准备公开；已公开工具为 `0.2.0.dev3`，功能发布提交为 [78daceca853574886de8ce77a53ad2b9b80dc8fb](https://github.com/Serenin0x/sushiro-queue-predictor/commit/78daceca853574886de8ce77a53ad2b9b80dc8fb)，42个远端文件与本机逐一核对，见 [PROJECT_HANDOVER.md](PROJECT_HANDOVER.md) E0049；保留已选浅/深Logo。dev2 的 35 文件发布核对见 [PROJECT_HANDOVER.md](PROJECT_HANDOVER.md) E0026；dev3 新增显式离线 capture-check/capture-import、捕获证实的固定 gateway 目录和 stores 私有文件/到期保护，主执行者 **212 项 / 0.431 秒**离线检查通过，独立复核的时钟问题已修，见 E0028。dev3已推送main，dev4新增私有文件零网络auth-status和共享到期保护，当前准备另行公开，不发布未选定的品牌草稿；没有 Release、服务器、小程序前端或预测服务，仍是接入验证阶段。

当前营业时段实证：电脑目录返回147条，三店60/30秒各两轮短窗完成。旧凭证到期后，本人正常启动HAR观察到 /api/1.3/initialize 返回新查询凭证，与后续Bearers本机精确匹配；固定单店候选显式导入revision2后，20:41三店电脑各新增一份成功详情，主库共18成功0失败，每店6份。未验签声明为20:32:49–21:32:49、本机保护21:32:19；接手时重新检查，不能沿用历史importable或成功状态。初始化登录码/签名/设备参数的正常生成与独立自动续期仍待验证，尚不能全天无人值守。历史到期拒绝/零请求保留；新结果不推全国完整覆盖或源新鲜度。

当前路线为 **本人新鲜正常 HAR → 离线检查 → 显式单店上下文导入 → 固定 gateway 单份快照/目录 → 营业期 60/30 秒有界试点**。所有真实查询明确 `--api-profile miniapp_gateway`，没有自动主机回退；正常查询凭证来源已观察到，独立正常参数生成未知，不自动续期。捕获 HTTP 200 与本机生成配置均不证明服务器现在接受，短窗成功也不代表全天稳定或 1.0。

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

### 2026-10-03 新 HAR 与目录证据

以下是 E0028 的夜间文件历史核对，当时没有电脑独立 GET，其他四条未进一步访问。之后营业时段的新文件、电脑查询和正常更新研究另记下节；原始 HAR 仍不进仓库/模型。

| 证据 | 已核对结果与限制 |
| --- | --- |
| 原始 index 0 西单详情 | 01:37:16+08:00，正常 GET `https://sapi.sushiro.com.cn/gateway/wechat/api/2.0/getStoreById?storeId=3004`，HTTP200，query/body ID与西单大悦城店一致；storeStatus=CLOSED、netTicketStatus=OFFLINE_CLOSED。停业值不当作零等待或源新鲜度 |
| 原始 index 2 目录 | 01:37:09+08:00，正常 GET 同固定基址 `/stores?latitude=1&longitude=1&numresults=10000`，HTTP200、直接数组147项，主执行者受限 normalize_directory 成功147项；不是 capture-check/import 的目录导入结果 |
| 新样本声明 | iat=01:13:05、exp=02:13:05（Asia/Shanghai）、相差3600秒，未验签；02:13:56首次接手已晚51秒，不泛化固定TTL或官方续期方式 |
| 实际 capture-check | exit0、entries_total=6、ignored_entries=5、唯一候选 index0西单3004/HTTP200/六头存在，matched=true、importable=false、capture_auth_expired；目录与其他路径均在该工具中忽略 |
| 实际 capture-import | 显式 index0/revision1/新目标，exit1、capture_auth_expired、network_performed=false；核对目标不存在，未写配置、发API或打开数据库 |

三家试点已在 [config/pilot-stores.json](../config/pilot-stores.json) 回填，下列名称/地址为本次目录身份对应：

| 用户试点 | 目录ID与公开身份 | 当前核验范围 |
| --- | --- | --- |
| 中关村大融城店 | 3014 / 中关村大融城店；北京中关村大街15号大融城西区A地块地下1层 | 当前 single_store_read_only；新增 60/30 秒短窗，长期与源刷新待验 |
| 西单店 | 3004 / 西单大悦城店 | 当前 single_store_read_only；新增 60/30 秒短窗，长期与源刷新待验 |
| 成都世豪店 | 2009 / 世豪广场店；成都剑南大道中段998号4层C414+C415 | 当前 single_store_read_only；新增 60/30 秒短窗，长期与源刷新待验 |

三店 `api_profile=miniapp_gateway`、`live_data_verified=false`。147项只说明本次目录响应可解析，不能当全国完整覆盖或147店详情权限；目录身份、单店只读和连续实时验收分别记录，不拿合成ID访问真实门店。

### 2026-10-03 营业时段电脑独立查询

完整过程与限制见 [PROJECT_HANDOVER.md](PROJECT_HANDOVER.md) E0036；以下是当前公开安全摘要。

用户交付的本轮文件有 1 条正常 GET，捕获于 19:17:43（Asia/Shanghai），西单 3004 / HTTP200 / 六项上下文头存在。显式 import index0、revision1 成功，committed=true、durability_confirmed=true；未验签声明 iat=19:17:02、exp=20:17:02。新旧 authorization 本机比较不同，其他五个上下文相同；这不证明官方更新端点、固定期限或无人值守更新。

正常主采集共 **15 份成功详情快照，0 失败**，另有一次目录 HTTP200 / 147 条 / 405ms。先分别核对三店，再 60 秒各两轮、30 秒各两轮；以下间隔来自同 run 相邻请求的起始时间，而不是响应到达差或上游更新时间：

| 门店 | 成功详情数 | 60 秒短窗实际起始间隔 | 30 秒短窗实际起始间隔 |
| --- | ---: | ---: | ---: |
| 西单 3004 | 5 | 59.992秒 | 29.987秒 |
| 中关村 3014 | 5 | 59.851秒 | 29.990秒 |
| 世豪 2009 | 5 | 59.782秒 | 30.006秒 |

主采集请求耗时 167–420ms。60 秒阶段为 19:22:21–19:23:22，30 秒阶段为 19:24:56–19:25:27。西单/世豪的展示集合出现变化，中关村在这两个相邻短窗的展示集合相同；字段和集合变化不能推实际处理桌数、真实过号率或源缓存周期。

用户在两阶段之间确认已退出寿司郎小程序、三个临时开关关闭，后续三店 GET 仍成功；19:23 独立镜像核对 Surge 顶部为“启动”，HTTP捕获/MitM/数据包捕获均OFF。电脑查询使用固定HTTPS请求，未通过手机界面或刷新取得每次数据。用户自行完成本轮捕获，实际开启时长未独立计量；本执行者没有开启捕获。

另做一次明确的只读对照：19:30:41.879 仅去掉 authorization，其余五头保持正常上下文，西单返回 HTTP401 / 400ms。无重试、无写数据库；这条不混入15份主采集质量报告。结果表明当前完整上下文仍需授权，不能以匿名查询替代正常更新，也不据此推其他头均必需。

所有 source_updated_at 仍 null、upstream_freshness=unknown，等待相关字段及-1单位未验收。三店配置新增 sampling_validation=short_window、每周期2轮，live_data_verified 继续false表示长期/源刷新边界。没有服务器、自动续期、模型、前端或业务写操作。

正常更新研究仅本机受限查看已交付旧 HAR 同域方法/安全路径/HTTP状态；观察到 GET /api/1.3/miniapp/login/status，返回白名单 data/status/data.status 的类型，未输出值/私人账号字段，未请求该端点或加入客户端。用户已获得完整启动的同域1分钟捕获指导：先结束微信进程，再打开三项、正常重开小程序/西单并刷新，立即关闭，导出整个本轮会话而非一条详情；遇登录/卡住先关闭捕获，再由本人完成正常流程。20:01完整启动文件已交付并检查（下节），仍未取得新authorization，不能把路径名当已实现续期。

### 2026-10-03 完整启动文件仍沿用有效凭证

完整过程见 [PROJECT_HANDOVER.md](PROJECT_HANDOVER.md) E0041。

用户报告西单刷新一次、会话约272KB，导出的HAR实际298480字节、7条，安全私有副本为0700目录内0600文件。全为sapi同域GET/HTTP200：西单固定详情index0/1分别20:01:23/20:01:22，目录及privateRoom/shopInfoList为20:01:13，login/status/home/citys为20:01:09（Asia/Shanghai）。具体操作次数和开启时长仍按用户报告区分；两条相邻详情不推周期采样或源刷新频率。

实际capture-check成功，entries_total=7/ignored_entries=5，两条详情六头存在、身份匹配、matched/importable均true；检查时仍在声明保护期限外。7条authorization与当前私有配置在本机逐一比较相同，声明仍为19:17:02–20:17:02；两条单店整组仅user_agent不同，不披露值。已查看的五个常见响应凭证头均未出现；login/status仍只记录data/status/data.status已知键类型，没有查看账号值或请求该路由。

西单两条捕获公共值相同：OPEN，groupQueuesCount=118/raw_wait=210（单位未知），booth/mixed为1742/1753/1755，reservation为7401/7411/7420、counter为空。只做离线捕获对照，不保存为新的live快照；主采集数据库仍为15成功0失败。没有导入新revision、增加API请求或取得自动更新证据。初次辅助脚本输出方法/函数签名使用错误已更正，正式CLI及本机比较完成，未改生产实现。

该次20:01捕获之后，已按一次到期后正常启动的有界计划取得新文件，并确认正常新凭证来源，见下节与E0048。当前不再要求重复相同条件刷新；人工捕获是协议验证步骤，未来采集仍由电脑/服务器固定查询承担。

### 2026-10-03 旧 revision 1 真实私有上下文的到期保护

20:29:20.853（Asia/Shanghai），当时revision1文件的正式stores路径实际返回exit1/auth_declared_expired/failure_phase=preflight/http_status=null，声明剩余0且未验签。对客户端构造、网络入口、数据库入口设置失败守卫，三者均0调用；无效CA也未被使用，主采集数据库未改。这是本机零请求保护，不是服务端401或精确失效验证，见 [PROJECT_HANDOVER.md](PROJECT_HANDOVER.md) E0045。

E0045当时20:26镜像连接暂停，随后本人交付到期后正常启动文件；新有效上下文已导入revision2并经电脑验证，见下节。旧revision1与20:01的历史importable状态不能继续使用；下一次更新须revision3或更高并重新检查当前保护时间。

### 2026-10-03 正常初始化的新凭证与第二次电脑验证

完整记录见 [PROJECT_HANDOVER.md](PROJECT_HANDOVER.md) E0048。本次本人明确交付的9条HAR只在本机有限处理，所有请求均为唯一授权sapi主机；私有副本303018字节、父目录0700/文件0600。没有将HAR或秘密传给模型，也没有重放任何初始化POST。

| 时间（Asia/Shanghai） | 正常小程序请求与观察 |
| --- | --- |
| 20:32:48 | GET /api/1.3/citys，旧查询授权，HTTP401 |
| 20:32:49 | POST /api/1.3/initialize，无authorization请求头，HTTP200；data.auth_token与后续七条请求中的新Bearer精确匹配 |
| 20:32:49–51 | citys/home/login/status、gateway initialize、stores、shopInfoList，共六条HTTP200，使用新查询授权 |
| 20:32:59 | 固定getStoreById?storeId=3004，HTTP200、店名/ID匹配、六项查询上下文完整 |

只记录初始化契约的白名单键、类型和存在性：

| POST /api/1.3/initialize 请求字段 | 本次可确认 | 仍待验证 |
| --- | --- | --- |
| long_token、js_code | 字符串且非空 | 正常来源、权限/有效期、是否必需及是否存在其他支持的更新流程 |
| ts | 整数，与捕获秒级时间相符 | 允许时差与校验规则 |
| nonce | 字符串 | 正常生成与复用规则 |
| sign | 字符串，64位十六进制 | 算法、输入和正式生成方式；形状不能证明SHA/HMAC算法 |
| device_id、app_version | 字符串 | 正常来源、必需性及校验规则 |

响应仅核对status/message/data与命名auth_token的类型、对应关系，不读取个人值。另一个 POST /gateway/wechat/api/2.0/initialize 请求含非空jsCode，与前一个js_code不同；响应顶层auth_token、expiry、expires_in=86400。该响应凭证与门店查询Bearers不同，**不能写成查询授权24小时或用于替换当前凭证**，实际用途待核验。

微信官方[Tencent API类型文档](https://github.com/wechat-miniprogram/api-typings/blob/master/types/wx/lib.wx.api.d.ts)中LoginSuccessCallbackResult说明wx.login返回code有效5分钟。本次两个命名字段来自wx.login仍是待验证推断；不能用保存旧码的方式承诺长期更新。本轮未验证一次性使用规则，也未证明仅long_token即可独立更新，未省略参数试探、猜签名或复制官方小程序秘密。

正式capture-check当时exit0、9条/忽略8条、唯一index0候选身份匹配/importable=true；显式capture-import index0/revision2当时exit0、written/committed/durability_confirmed均true。两者都零网络，导入只证明本机文件提交。新查询凭证的未验签声明20:32:49–21:32:49、声明3600秒，本机保护21:32:19；不泛化固定TTL。

其后新启动collect执行三店各一轮并实际成功：

| 店铺 | 请求起始→收到（20:41） | 耗时 | booth / mixed展示 | reservation展示 | count / raw_wait（单位未知） |
| --- | --- | --- | --- | --- | --- |
| 西单3004 | 55.034→55.377 | 342ms | 1935 / 1936 / 1937 | 空 | 77 / 105 |
| 中关村3014 | 55.386→55.756 | 370ms | 2069 / 2084 / 2086 | 7450 | 15 / 25 |
| 世豪2009 | 55.762→56.126 | 364ms | 601 / 602 / 603 | 7431 | 10 / 10 |

三店均OPEN/ONLINE/ON，counter展示空、waitTimeCounter=-1、waitTimeCap=180，source_updated_at=null。collect exit0，报告复核主库18成功0失败、每店6份，profile=miniapp_gateway/data_origin=live；手机捕获的401没有混入主库。这是新进程使用更新配置的成功，不是实际运行中热切换或持续跨到期无人值守验收。

下一步优先核对登录码正常取得方式、签名协议、设备参数、long_token作用和正式接入可行性。该次人工捕获目的已完成，暂不需要再刷新；独立自动更新、持续采集、限流和源新鲜度仍未验收。下一次导入须revision3或更高，在当下重新检查声明保护。

## 门店 ID、查询凭证与逐次生命周期

门店 ID 是标识，不是登录凭证；本次西单 3004 没有到期证据。会过期的是查询鉴权。每份新凭证都要记录声明签发/到期、实际查询成功/失败的时间边界及正常续期结果，不因一次样本宣称固定 TTL。2026-10-02 的既有样本本机仅解析 iat/exp，声明签发为 2026-10-02 19:44:37、到期为 20:44:37（Asia/Shanghai），相差 3600 秒；20:21:25 捕获时剩余 1392 秒（23 分 12 秒）。未验证签名，不把声明当作服务端保证，也不将 20:45 的 401 唯一归因于该声明。

2026-10-03 新样本声明为 01:13:05 签发、02:13:05 到期，首次接手 02:13:56 已晚51秒；捕获当时 HTTP200 不等于接手时可用。本轮没有用失效值试探服务端，也没有新运行配置。第三份声明为19:17:02–20:17:02、第四份为20:32:49–21:32:49，均曾显式导入并通过电脑查询；第四份正常来源已观察到初始化响应。四份声明相差3600秒仍不能推固定一小时、精确服务器边界或独立自动更新。

纯本机零网络auth-status已实现。dev4新增--credentials-file，严格读取显式整组私有文件，不补环境，沿用profile/schema/权限验证；文件读取后取当前时间，并与stores/snapshot/collect共用30秒声明保护。省略文件参数保持原环境模式及输出，只读对应授权变量，不读取其他头或CA。

```sh
PYTHONPATH=src python3 -m sushiwait auth-status --api-profile miniapp_gateway --credentials-file "$SUSHIWAIT_CONTEXT_FILE"
PYTHONPATH=src python3 -m sushiwait auth-status --api-profile miniapp_gateway
PYTHONPATH=src python3 -m sushiwait auth-status --api-profile legacy
```

文件模式额外输出credential_source=private_file、credential_revision、checked_at、network_performed=false、server_acceptance=unverified，以及authorization_guard.state/stop_reason/margin_seconds。state为stop、no_declared_stop或unknown；声明剩余不超过30秒为auth_expiring，已过期为auth_declared_expired，乱序为auth_claims_invalid。no_declared_stop只表示当时没有声明保护原因，不保证服务端接受；unknown不推TTL或无限有效。各次命令独立读取文件，不保存跨进程revision历史；不写文件/数据库、不登录或续期。

**exit0/ok=true仅表示检查完成，stop/unknown也返回exit0**；文件缺失/损坏/权限不安全/profile不符返回固定安全错误/exit1，不能回退环境。完整说明见[凭证更新证据](AUTH_REFRESH.md)。dev4新增14项并通过全套226项/0.466秒；21:19:14.415真实revision2检查返回remaining814秒/no_declared_stop，客户端/CA/opener/socket/数据库0调用、环境凭证读取0，私有文件和主库未改。接手时当下重查，不能沿用该剩余时长。

环境模式仍只含原ok、api_profile、configured、token_kind、declared_issued_at、declared_expires_at、declared_lifetime_seconds、remaining_seconds、expired、expiry_source、signature_verified字段。UTC声明与实际请求时间分别解释，未验签声明不当服务端保证；此前dev1环境模式真实核验保持原编辑历史。

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
| miniapp_gateway | `https://sapi.sushiro.com.cn/gateway/wechat/api/2.0` | getStoreById；dev3新增已捕获的 stores 固定 GET，参数仅 latitude=1、longitude=1、numresults=10000 |
| legacy | `https://crm-cn-prd.sushiro.com.cn/wechat/api/2.0` | stores、getStoreById；旧目录匿名测试为 401，作为历史诊断能力保留 |

2026-10-03 本人正常请求捕获已证实 gateway stores 的路径与上述固定参数，dev3只添加这条已观察路线，不开放任意参数或URL。三店ID由目录身份核对；详情仍需 query/storeId、响应 id 与店名一致，另两店权限不能由目录存在推断。全国完整覆盖仍待核验，不猜数字ID或用合成ID访问真实服务。

正常查询授权来自允许的接入方式或用户正常使用流程，本机注入，不能把值放入聊天、命令参数、README、Issue、提交或模型。gateway 与 legacy 配置独立，不将旧凭证转发到新主机；网络客户端不自动读取 HAR、微信文件或开源共享 token；新 capture 命令只离线处理用户明确指定的本机文件。新请求本次成功组合保留正常上下文头，各项服务端必需性尚未逐项验证：

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

### 显式私有查询上下文文件（dev3）

`--credentials-file` 由 **stores、snapshot、collect及dev4的auth-status** 支持，查询命令与--anonymous互斥。不传文件时仍用上表相互隔离的环境变量；查询环境模式缓存本次进程初始上下文，修改环境需重启。auth-status指定文件只检查该整组，省略文件只检查环境，两种来源不拼接；状态检查不读取CA。网络查询的文件模式不从环境补授权或五项上下文头，只有可信CA的`SUSHIWAIT_CA_FILE`继续由本机环境配置。

文件由用户本人已授权的正常查询流程在本机受控生成。采集命令只读取显式上下文文件，不寻找登录态或猜 refresh；capture-import 可按下节规则显式离线生成该文件。不要将原始 HAR 当作这个 JSON 文件，不把任何头值粘贴到聊天、命令参数或仓库。以下只描述字段契约，不提供令牌示例：

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

两文件在同目录、同文件系统时该 rename 为原子替换；目录 0500 或文件 0400 可用于只读输入，但准备更新需要合法的目录写权限。网络采集命令不负责取得新上下文或写这个文件；dev3的显式 capture-import 可以从仍通过声明保护的指定单店GET生成它。revision 检查仅在当前进程：禁止倒退，同 revision 内容不同为冲突，同 revision 内容相同可继续；重启后不保留上一进程的最大 revision。revision 递增只说明本机配置变化，不证明官方完成续期。

每个 GET 前重新读取整组文件，包括同轮的不同门店；上下文有变化时先完整构建新的客户端再替换，每个 GET 只用一组。等待中最多每 60 秒重读，并按声明到期前的保护期限提前唤醒。由此可以接收本机正常更新，但官方更新来源与自动续期仍未知。

stores/snapshot/collect 请求前检查声明 iat/exp，未验证 JWT 签名；声明无效返回 auth_claims_invalid，已到期为 auth_declared_expired，剩余不超过 30 秒为 auth_expiring。**这些都是零网络的本机停采**，不是上游 401。等待到了保护期限仍无有效更新就停止，文件读取或配置错误也停止整次采样。opaque、缺失 exp 等未知到期不填默认 TTL，也不表示服务器会接受；真实 GET 失败仍首错停止、没有自动重试。已有在途 GET 不会因为文件替换而混入新头，下一次 GET 再检查。

### 从本人正常 HAR 离线检查与导入（dev3）

只处理用户明确指定、允许范围内的本机导出文件。capture-check 和 capture-import **零网络、不开数据库、不启动捕获/登录/续期**；原始文件含秘密，保留本机私有位置，不上传聊天、在线HAR站或仓库。用户已知 fresh HAR 流程继续限 sapi 单域名、最多1分钟正常刷新后及时关闭启动/MitM/捕获三项；捕获由用户操作；主执行者随后只读查看镜像及打开Surge面板核对关闭状态，没有开启捕获。

输入仅支持安全POSIX文件读取：HAR ≤8 MiB（8388608字节）、≤100条，响应正文≤2 MiB（2097152字节）；拒绝祖先/文件符号链接，文件必须当前用户的常规单硬链接文件，并核对读前后及父路径身份。正常导出源可能0644或位于非私有父目录，工具不强制源0600/0700、不改其权限；本轮受控副本自身为0700目录/0600文件，后续仍应私有保管。严格UTF-8/JSON，拒绝重复键、非有限数字；响应支持普通UTF-8或严格规范base64，不接受任意编码。

候选仅匹配 `https://sapi.sushiro.com.cn/gateway/wechat/api/2.0/getStoreById?storeId=...`：正常GET、无请求正文、仅规范数字storeId查询、queryString与URL一致（如存在），带时区捕获时间、HTTP200及响应Store身份对应。目录、其他主机/路径仅计ignored，不解析或导入其上下文，也不显示其地址/内容；目录147项的证据来自主执行者另行受限白名单核验。

用户在本机设置 `SUSHIWAIT_CAPTURE_FILE` 为已授权HAR的实际路径后，可检查：

```sh
PYTHONPATH=src python3 -m sushiwait capture-check --har "$SUSHIWAIT_CAPTURE_FILE"
```

输出 schema_version=1 / data_origin=capture、entries_total、ignored_entries与candidates；每个候选仅有原始0基entry_index、UTC毫秒captured_at、HTTP状态、匹配后的store_id/store_name、六个header的存在布尔值、authorization_status安全时间白名单、matched/importable/error_code。matched表示请求/时间/200响应身份匹配，缺头或过期仍可能matched=true；importable还须上下文合法且当前声明保护通过。network_performed=false / network_verified=false / server_acceptance=unverified 明示历史捕获不是新的请求或当前服务端验收。**exit0只表示检查完成，不保证存在可导入候选**；输入拒绝为固定安全错误/exit1，不显示原始异常、私有路径或头值。

对一份**新鲜**样本，先从候选明确选原始index，并在本机设 `SUSHIWAIT_CAPTURE_ENTRY_INDEX`、`SUSHIWAIT_CONTEXT_REVISION` 与上节私有输出路径；revision为1..2^63-1，新文件可从1开始，更新既有目标必须大于其revision。以下只在候选允许且调用范围核实后执行，不是本轮已执行成功记录；本轮index0已过期不可导入：

```sh
PYTHONPATH=src python3 -m sushiwait capture-import --har "$SUSHIWAIT_CAPTURE_FILE" --entry-index "$SUSHIWAIT_CAPTURE_ENTRY_INDEX" --output "$SUSHIWAIT_CONTEXT_FILE" --revision "$SUSHIWAIT_CONTEXT_REVISION"
```

该命令重新读取HAR，**不自动选择、不从目录导入、不同条目之间不拼头**。六项上下文来自同一单店GET，缺失头明确null、不读环境；以完整schema1九键私有文件写入，≤16KiB。输出父目录必须当前用户、权限0700；既有目标须为同用户单硬链接常规私有文件0600/0400，新文件0600。不要给输出使用符号链接或多人可写目录。

生成初检和最终提交前均重新读取当前本机时间：invalid_claim、声明已到期、剩余不超过30秒分别capture_auth_invalid_claim/capture_auth_expired/capture_auth_expiring，均拒绝提交；缺失exp或opaque到期保持未知，不推默认TTL，也不证明服务器接受。写入期间跨入保护期限也拒绝，不沿用初检冻结时间。独立复核发现过冻结时钟问题，已由实际CLI慢写回归覆盖31秒推进到30秒的拒绝边界。

私有生成使用同父目录独占0600临时文件、文件fsync/有界读回、目标/父路径复核、协作目录锁与原子rename。**rename提交前失败保留旧目标**；成功输出written=true、committed=true、durability_confirmed布尔值、revision、身份/捕获时间与安全声明时间。若提交后目录fsync失败，仍明确committed=true、durability_confirmed=false、exit0：新文件已提交，仅持久化确认不足，不能当“未写入”盲目重试或回滚。POSIX无CAS，不守协作锁的同用户写者仍有最终复核至rename的竞争窗口；避免同时使用其他进程改同一目标。

常见拒绝包括capture_auth_expired、capture_invalid_request/response/context、capture_entry_not_found、capture_destination_unsafe/changed、capture_revision_conflict或capture_write_failed，输出仅固定类别、api_profile与network_performed=false / exit1；缺必需CLI参数exit2。生成配置成功不代表官方续期或server_acceptance已验证，下一步仍需在允许范围执行单次真实只读查询。

### 固定 gateway 目录与单店运行

目录现已有营业时段电脑独立GET实证：HTTP200、147条。以下是目录筛选方法；stores也执行请求前到期保护，预检只输出failure_phase=preflight、checked_at、http_status=null和安全时间，不开数据库：

```sh
PYTHONPATH=src python3 -m sushiwait stores --api-profile miniapp_gateway --credentials-file "$SUSHIWAIT_CONTEXT_FILE" --match 中关村 --match 西单 --match 世豪
```

新 profile 和此前实际 dev1 CLI 单店保存已核验。符合契约的私有文件与可信 CA 就绪后，西单已验证 ID 的单次命令为：

```sh
PYTHONPATH=src python3 -m sushiwait snapshot --api-profile miniapp_gateway --credentials-file "$SUSHIWAIT_CONTEXT_FILE" --store-id 3004 --db data/local/gateway-pilot.sqlite3
PYTHONPATH=src python3 -m sushiwait report --db data/local/gateway-pilot.sqlite3
```

2026-10-02 实际尝试 60 秒 / 2 轮采样，首轮在 20:45:16.961 得到 HTTP 401 后立即停止，没有第 2 轮或周期等待；30 秒阶段未执行，60/30 秒采样均未验收。report 成功读取同店同 profile 的 1 成功与 1 失败。到期静态凭证不再重试；从本人正常查询流程取得新的本机上下文，检查声明时间与单次实际可用性后，才进入下列有界验证。先执行 60 秒；仅在其成功、无异常且调用范围允许时再执行 30 秒。该2026-10-02失败保持历史；2026-10-03已按同样固定路由对三店完成60/30秒各两轮，实证见上节。以下仍为可复用的单店操作示例：

```sh
PYTHONPATH=src python3 -m sushiwait collect --api-profile miniapp_gateway --credentials-file "$SUSHIWAIT_CONTEXT_FILE" --store-id 3004 --interval 60 --samples 2 --db data/local/gateway-pilot.sqlite3
PYTHONPATH=src python3 -m sushiwait collect --api-profile miniapp_gateway --credentials-file "$SUSHIWAIT_CONTEXT_FILE" --store-id 3004 --interval 30 --samples 2 --db data/local/gateway-pilot.sqlite3
```

上述文件命令只有在本机已经准备好完整私有文件后才能执行；省略 --credentials-file 可继续使用对应 profile 的环境模式，但更新环境需重启。dev3继承的重读/到期保护已离线验证，2026-10-03 新鲜上下文下的三店60/30秒短窗实测已完成；运行中换成另一份新上下文、独立正常参数生成/自动更新与完整实际有效边界仍须另行记录，不靠连续重启或失效值重试代替正常更新验证。Ctrl-C 返回 interrupted / exit 130；修正本机问题或取得正常更新后可用同一 --db 继续追加历史，每次运行有新 run_id、轮数重新开始，**不能恢复上次未完成任务的采样游标/剩余轮数**。

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

工具只允许两个固定 profile 的已知只读 GET，dev3 gateway支持已观察的详情和固定参数目录；无登录、个人号单、取号、预约、取消或重排接口。不跟随重定向或读取环境代理，15 秒 I/O 超时、2 MiB 响应上限、失败不自动重试。没有守护进程、多实例锁、通知或全天调度，本机不要同时运行多个采集进程。

## 电脑试点批量与后期同步展示

用户要求在电脑快捷批量测试门店，取得有效本人查询授权并核实目标 ID 后有节奏执行，不需要逐店手机抓包。当前 collect 可重复传 --store-id，但最多 1–3 店、每店 1–120 轮，首个失败停止；这是试点批量，目录已有147项捕获证据，全国完整覆盖和全国批量仍待实现/验收。同一 profile 的多店权限也须核实，不能从西单一次成功推断所有门店均可访问。三家ID现已分别通过详情查询及60/30秒短窗验证；以下为可复用的多店操作示例，继续检查有效上下文与允许频率。单次和短窗成功不能泛化全天或其他门店权限：

```sh
PYTHONPATH=src python3 -m sushiwait collect --api-profile miniapp_gateway --credentials-file "$SUSHIWAIT_CONTEXT_FILE" --store-id 3014 --store-id 3004 --store-id 2009 --interval 60 --samples 1 --db data/local/gateway-pilot.sqlite3
```

后期本项目小程序需要同步显示/刷新所选门店当前堂食与预约叫号，复用服务端共享采集，让用户减少在两个小程序之间来回切换。仍按有限展示集合保存与显示，分清请求接收时间和未知的源更新时间，明确刷新失败/过期状态，不把旧数据或失败当作无人排队。此项属于 R23，须等 R18 的真实字段、刷新与连续接入齐备后才实现前端；当前没有服务端持续调度或小程序展示功能。

## 输出与本地存储

命令输出 JSON；数据库仅保存规范化公共观察与安全失败分类，文件在 `data/local/`，默认 `sushiwait.sqlite3`。该目录被 Git 排除。posix 环境新目录 0700、数据库 0600；拒绝符号链接数据库，未知数据库版本不覆盖。

- 规范化快照 `schema_version=1` 保持；它与数据库和 report 的版本独立。SQLite `PRAGMA user_version=2`，样本保存 run_id、store_id、data_origin、api_profile、received_at、ok 与规范化 JSON；当前dev4全套226项离线检查通过；此前实际dev1 CLI使用schema2数据库成功保存。本轮没有新增数据库迁移。
- 观察、目录项与变化输出顶层 api_profile，仅允许 legacy / miniapp_gateway，不保存任意来源 URL；端点固定映射由客户端负责。compute_change 只有同店、同真实/合成来源、同 API profile 才比较，报告也按三者分组。
- 写打开旧 v1 数据库在显式事务中为旧成功/失败行补 legacy 列与 JSON 标签，保留行 ID、run_id 与内容；冲突 profile 或坏 JSON 拒绝并回滚。只读打开 v1 不迁移，使用虚拟 legacy 视图；report 的 schema_version=2 表示输出格式，database_schema_version 单独反映磁盘库版本。
- normalized 字段分别记录 present / missing / null / invalid。缺失不会变成 0，缺失队列不会变成空数组。
- 保留四类 groupQueues 的完整字符串数组、顺序与重复情况，不将展示最大号码当作全局游标。
- `raw_wait` 与 `groupQueuesCount` 单位均为 unknown；未经小程序/现场对照，不解释为分钟、人数或已签到桌数。waitTimeCounter / waitTimeCap 另按有符号整数保存 presence/value/unit=unknown，拒绝 bool，保留 -1 等原值；不猜测 -1 的禁用含义或正值的分钟单位。
- waitTimeCounter / waitTimeCap 的缺失、null、invalid 分别记录，公共哈希与标量变化覆盖两字段；旧快照缺新键按 missing/None 比较，不当 0、不修改历史 JSON。有符号等待字段与本机auth-status的既有回归保持；当前dev4全套226项通过，快照schema_version仍为1。
- `request_started_at`、`received_at`、`elapsed_ms` 是本地请求时间；`source_updated_at=null`，`upstream_freshness=unknown`。
- content_hash 只作用于规范化公共字段；相同 hash 仅表示所保存字段相同，不能证明缓存或源新鲜度。
- 未知字段记录安全键名清单；未知对象、个人字段值和原始响应不落库。如果以后发现有用字段，再核实语义、权限并加入白名单。
- 失败不生成成功快照；GET 返回错误为 failure_phase=request，收到正常响应但规范化失败为 normalization。本机文件/声明/配置保护为 preflight，只保存 checked_at、http_status=null 和安全鉴权时间元数据，不虚构请求开始/响应接收/耗时；三者都使 collect 首错停止。数据库沿用 received_at 列保存事件时间，preflight 行在此列保存 checked_at。

### 只读质量报告（dev2继承，dev3格式保持）

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

1. **正常上下文更新来源。** 19:17新上下文已导入并通过实际查询，但正常自动更新仍未知；下一份应捕获完整启动流程，限已授权sapi和1分钟，完毕关闭三个开关。只研究正常路径，不猜登录/refresh端点；声明不推固定TTL。
2. **显式整组更新与边界。** 新正常单店GET通过当前声明保护后，capture-import明确原始index/output/递增revision；不能目录导入、自动选条目或拼头。当前本机文件revision1，下一份更新必须更大；committed/durability_confirmed分别解读，文件模式由stores/snapshot/collect预检。
3. **保持三店的实际能力范围。** 3014/3004/2009均完成详情身份和短窗采样，single_store_read_only+sampling_validation=short_window；live_data_verified均false表示长期/源刷新未验收。目录147条不证明全部门店详情权限或全国完整覆盖。
4. **补齐页面和真实结果对照。** 有效凭证期间电脑查询已独立于小程序；接下来逐项核实完整堂食/预约号码、签到桌数、等待字段单位/-1、missing/null/invalid、来源时间。实际60/30秒请求节奏已验证，不等同源更新延迟或限流允许上限。
5. **保留失败与证据口径。** 15份主采集成功/0失败，另一次省略授权头对照401不混入数据库；到期/坏文件本机停采与request/normalization失败分开。均首错停止、不重试，report区分全历史/有限窗口，不推ETA、真实过号率或缓存周期。
6. **扩大持续验收再部署。** 覆盖正常更新、完整到期边界、限流/TLS/网络/文件故障和停机结果，形成长期可靠性证据。当前没有官方自动续期、全天调度、任务游标恢复或服务器部署；同库重启仅续写历史，两轮与147目录项不替代可靠性或1.0验收。

实际操作的凭证与原始网络记录留本机受控位置，公开仓库只记录无秘密的结构、证据与结论。本人账号授权不包含真实取号、取消或重排。

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
