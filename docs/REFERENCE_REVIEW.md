# 参考项目研究与可借鉴范围

研究日期：2026-10-02，Asia/Shanghai。本记录基于公开源码与作者文档的只读核对；没有安装或运行参考项目，没有调用寿司郎生产接口，也没有验证公开查询凭证是否有效。以下“源码存在”不代表接口目前可用，更不代表获得第三方服务承诺。

完整需求、版本状态和编辑记录以 [PROJECT_HANDOVER.md](PROJECT_HANDOVER.md) 为准。本项目的只读工具为原创实现，没有复制参考项目代码；下述研究与本项目实际接入验证分别记录。

## 2026-10-06 下午：正常下载客户端与独立匿名查询

用户恢复主线并允许正常操作已连接手机后，核对实际选中的「寿司郎小助手」正常下载版本23，仅以自写内存解析/语法树检查请求结构，未执行、复制或公开第三方源码，未读账号存储或认证材料。结构中有直接CRM `/api/1.1/remote/groupqueues?storeid=`、`storequeuecount?storeid=` 调用，移动端分包调用两方法并绑定storeQueue/boothQueue/reservationQueue至展示。包格式概念来自此前锁定[f1tz说明](https://github.com/f1tz/wxunpack/tree/cdedad11c3e8dff7e72338e2b491e3db62439459)，无许可代码不复制；本项目适配器原创。

本项目电脑独立匿名GET对三试点队列及数量返回HTTP200，不带微信凭证、Cookie、Referer或应用身份头，TLS验证保持、无重定向/重试。新路径不同于此前匿名401的旧wechat目录；不能以401推CRM所有接口均需短时凭证。五数组中storeQueue不同于mixed，原始数量为整数、单位仍unknown，两查询不是原子快照，响应未带可核验门店身份或源更新时间。工具与边界见[独立来源说明](ANONYMOUS_QUEUES.md)、实际过程见E0213–E0214。

手机正常点刷新后仍显示较早号码，本轮只看连接摘要且未找到业务域名，没有对应手机请求内容或同次数值匹配；不能将静态地址断言为该次手机实际流量、证明完全后台架构、官方合作或永久免认证。摘要窗口越时与已OFF/筛选恢复如实记录；无独立手机限时关闭前不继续观察，解密和磁盘捕获未开。下文午间判断保留其时点，不覆盖新证据。

## 2026-10-06「寿司郎小助手」实屏补充

用户在电脑微信打开名称准确为「寿司郎小助手」的小程序。正常界面有消费计算器、门店排队及鲜知道；门店排队提供多城市列表及排队查询。打开中关村大融城店后可见当前等待桌数、预计等待、门店排队三位、即将带位的普通/预约各三位和刷新按钮。本次电脑版数值均为占位符，实际点击刷新明确提示“请使用移动端进行排队查询”。用户随后确认手机正常刷新能够显示号码，属于用户提供的运行观察，尚未取得该次请求的独立证据。

仅这些页面不能确定查询来自小助手自有后台、手机直接访问上游或正式数据合作，也不能认证后台长期更新凭证的机制。页面的“门店排队含新签号、预约号以及返签号”和“即将带位不含返签号”属于该第三方展示说明，不能据此改写本项目字段语义。未取号、保存消费记录、授予新权限、联系开发者或捕获第三方请求；没有复制其代码、密钥或使用第三方共享凭证。

公开搜索另找到[寿司郎取号小助手](https://sushiro.chinatsu1124.com/)，作者页面称每3分钟自动更新，并描述两种时间推算。名称、界面不同，尚无同一运营主体证据，不能把该网页的刷新周期、功能或实现归给用户打开的小程序；网页说明也不等于本项目实测或其长期认证实现证明。用户曾同意仅观察正常手机刷新对应的域名和时间，HTTPS解密与磁盘捕获保持关闭、最多60秒，步骤已提供；随后明确将手机研究留到下次，先完成正在运行的采集并暂停目标。本轮未读到手机镜像页面或拿到对应域名，不把手工“看不到”解释成请求不存在，不扩大既有单域名解密范围。完整记录见E0208–E0209。

## 结论

两个项目都提供了“从后端查询实时信息”的技术线索，不能据此认为已经具备我们需要的完整大陆监控系统。

| 维度 | sushiro-hk-live | sushiro-cli | 本项目的处理 |
| --- | --- | --- | --- |
| 地区 | 香港 SUSHI-PASS | 大陆后端查询线索 | 地区适配独立，香港请求不能直接替代大陆请求 |
| 实时采集 | HTTP 查询分队列号码 | 门店列表、详情与时段查询 | 验证大陆详情完整字段及来源延迟 |
| 字段保留 | 多种队列数组 | Store 类型未保留 groupQueues 等字段 | 保留允许保存的完整原始字段，再建立明确映射 |
| 等待估算 | 固定每组分钟系数 | 展示 wait，作者定义为等待桌数 | 用真实叫号结果训练并校准时间区间 |
| 持续监控 | 网页轮询、外部定时通知 | 单次 CLI / stdio MCP 操作 | 实现持久后台调度和门店共享采集 |
| 个人操作 | 未见大陆个人取号流程 | 查询/业务凭证隔离，有预约操作设计 | 立即取号、取消、重排分别验证 |
| 许可 | 未发现明确许可证 | MIT，另有上游归属记录 | 不复制无许可代码；引入有许可代码时保留归属 |

## 1. 香港项目 sushiro-hk-live

仓库：[scmlewis/sushiro-hk-live](https://github.com/scmlewis/sushiro-hk-live)。查看的 main 提交为 [`9cf24d5ddf002c39b0c3b185cfc0be666798ad07`](https://github.com/scmlewis/sushiro-hk-live/commit/9cf24d5ddf002c39b0c3b185cfc0be666798ad07)。研究时仓库未归档，仓库元数据的 license 为空，递归文件树中未发现 LICENSE 文件。

### 已核对的请求和字段

[`api/_lib/cache.ts`](https://github.com/scmlewis/sushiro-hk-live/blob/9cf24d5ddf002c39b0c3b185cfc0be666798ad07/api/_lib/cache.ts) 直接查询香港后端 `sushipass.sushiro.com.hk`：

- 门店列表路径为 `/api/2.0/info/storelist`，参数包含 `region=HK`。
- 队列路径为 `/api/2.0/remote/groupqueues`，参数包含地区和门店 ID。
- 代码使用通用请求头，没有个人微信令牌；不能据此推断大陆接口也无需鉴权。
- 返回处理涉及 storeQueue、boothQueue、counterQueue、mixedQueue、reservationQueue、storeCounterQueue、storeBoothQueue、reservationCounterQueue、reservationBoothQueue、separateQueue 等分队列数组，类型见 [`src/types.ts`](https://github.com/scmlewis/sushiro-hk-live/blob/9cf24d5ddf002c39b0c3b185cfc0be666798ad07/src/types.ts)。这些字段的香港业务含义不能直接套到大陆。

### 可借鉴的工程思路

- 列表缓存 30 秒、队列缓存 15 秒、请求超时 8 秒；响应区分缓存、旧数据和错误。时间戳为本地请求相关时间，不等于上游更新时间。
- [`src/App.tsx`](https://github.com/scmlewis/sushiro-hk-live/blob/9cf24d5ddf002c39b0c3b185cfc0be666798ad07/src/App.tsx) 按收藏/比较门店轮询，页面隐藏时停止；实际配置为 30 秒，README 所述 10 秒与当前实现有出入。
- [`api/notify.ts`](https://github.com/scmlewis/sushiro-hk-live/blob/9cf24d5ddf002c39b0c3b185cfc0be666798ad07/api/notify.ts) 与 [`SETUP.md`](https://github.com/scmlewis/sushiro-hk-live/blob/9cf24d5ddf002c39b0c3b185cfc0be666798ad07/SETUP.md) 使用外部定时触发和 Redis 订阅索引，按门店聚合关注者，同店查询复用。文档中 5 分钟触发周期达不到我们最后 15 分钟每 30 秒监控的要求。

### 必须重新设计的部分

1. 缓存是进程内 Map，不适合多个服务实例共享；网页关闭后的采集要由服务器独立承担。
2. 无缓存时请求失败可返回空数组，API 包装仍可能标 success；不能让模型把失败解释成“没有排队”。
3. [`api/_lib/notify-logic.ts`](https://github.com/scmlewis/sushiro-hk-live/blob/9cf24d5ddf002c39b0c3b185cfc0be666798ad07/api/_lib/notify-logic.ts) 用 mixed 队列最大号码计算差值，并将差值小于等于零判断为叫号；大陆有限的已签到展示集合不能支持这个推断。
4. [`StoreDetailModal.tsx`](https://github.com/scmlewis/sushiro-hk-live/blob/9cf24d5ddf002c39b0c3b185cfc0be666798ad07/src/components/StoreDetailModal.tsx) 采用号码差乘固定约 1.35 分钟的估算，较远队列有约 1.3 的系数；未形成我们要求的历史、日期和异常加速概率模型。
5. 通知流程未充分拦截旧数据，没有完整的持久提醒阶段去重；叫号时可能在推送失败后移除订阅。本项目需要发送状态、去重、可靠重试和送达结果记录。
6. 没有解决大陆业务登录、立即取号、取消重取或个人计划的持久恢复。

**许可处理：**研究接口和架构概念，但不复制该仓库代码、素材或文档。需要直接复用时必须先取得适用许可并记录。

## 2. 大陆命令行项目 sushiro-cli

仓库：[lmxx1234567/sushiro-cli](https://github.com/lmxx1234567/sushiro-cli)。查看的 main 提交为 [`982bebfb92fb57f017f9f27878cfd467f1da0410`](https://github.com/lmxx1234567/sushiro-cli/commit/982bebfb92fb57f017f9f27878cfd467f1da0410)。研究时未归档，采用 MIT 许可，作者记录了与 sushiro-overdose 的来源关系。

### 已核对的请求和字段

[`internal/api/client.go`](https://github.com/lmxx1234567/sushiro-cli/blob/982bebfb92fb57f017f9f27878cfd467f1da0410/internal/api/client.go) 使用大陆后端基础路径 `https://crm-cn-prd.sushiro.com.cn/wechat/api/2.0`，包括门店列表 `stores`、单店详情 `getStoreById` 和可预约时段查询等。这里只记录目的地址与协议线索，不保存源码里的任何查询凭证。

[`internal/api/types.go`](https://github.com/lmxx1234567/sushiro-cli/blob/982bebfb92fb57f017f9f27878cfd467f1da0410/internal/api/types.go) 的 Store 类型保留 id、name、address、area、storeStatus、reservationStatus、wait 等字段，未保留 groupQueues、groupQueuesCount、netTicketStatus 或完整最新放号/队尾信息。Go 的类型化 JSON 解析会丢弃未定义字段，所以现有 CLI 的 JSON 输出不能直接成为我们完整的叫号快照源。

作者 README 将 **wait 定义为等待桌数**，而 overdose 的应用代码将其映射为 WaitMinutes。两者未提供官方单位协议，因此本项目保存 raw_wait 并标记 unit=unknown；不能据此确定分钟、桌数或已签到桌数。响应与页面仍需对照；原类型用整数也不能区分缺失与真实零值。

### 可借鉴的工程思路

- 公共查询模式与个人/写操作隔离；QueryAuthorization、ReservationAuthorization、WechatID 分开，不能混为一个登录态。见 [`internal/api/public.go`](https://github.com/lmxx1234567/sushiro-cli/blob/982bebfb92fb57f017f9f27878cfd467f1da0410/internal/api/public.go)。
- 固定生产目的地址、禁止重定向、15 秒超时、2 MB 响应上限和脱敏错误；这些有助于避免凭证被转发到其他地址。
- 不对业务写入自动盲目重试；通信错误和服务端错误可能意味着结果未知，而不是确定失败。
- [`internal/service/service.go`](https://github.com/lmxx1234567/sushiro-cli/blob/982bebfb92fb57f017f9f27878cfd467f1da0410/internal/service/service.go) 在不确定结果后使用只读对账；取消须核对准确 ID 和明确的 CANCELLED 状态。这是值得借鉴的思路，但其现有对账路径不能直接认定为可靠。
- ticket/status 处理要求 netTicket、reservationTicket 字段存在，将明确 null 与缺失或格式错误区分。完整字段验证适用于我们的数据适配器。

### 必须补全或重新验证的部分

1. [`docs/limitations.md`](https://github.com/lmxx1234567/sushiro-cli/blob/982bebfb92fb57f017f9f27878cfd467f1da0410/docs/limitations.md) 明确尚未完成个人状态与预约创建/取消的端到端验收；旧 getReservations 对账路径有 404 报告。源码有恢复流程不等于恢复已经成功验证。
2. 没有提供原生微信业务登录；公共凭证是否持续有效、可分发或允许调用并未得到本项目确认。
3. 预约与立即排队取号是不同流程；未见我们需要的 createNetTicket / cancelNetTicket 完整实现。
4. CLI / stdio MCP 是调用工具，不是后台采集、实时预测或防过号服务；需要另建历史存储、调度、异常检测和个人计划。
5. [`docs/configuration.md`](https://github.com/lmxx1234567/sushiro-cli/blob/982bebfb92fb57f017f9f27878cfd467f1da0410/docs/configuration.md) 的个人配置是本地明文文件，macOS / Linux 权限为目录 0700、文件 0600，Windows 持久化支持有限；云端多用户系统需要独立的加密存储、租户隔离和访问控制。

**许可处理：**可研究或在核实 MIT 及 [`docs/provenance.md`](https://github.com/lmxx1234567/sushiro-cli/blob/982bebfb92fb57f017f9f27878cfd467f1da0410/docs/provenance.md) 后依法复用，并保留上游归属和第三方声明。本轮未复制任何实现；MIT 不赋予后端接口或数据的调用许可。

## 3. 其他大陆线索

此前已阅读 [Ryujoxys/sushiro-overdose](https://github.com/Ryujoxys/sushiro-overdose) 的应用与采集器材料；2026-10-02 开发只读工具前进一步锁定提交 [`e273df046789773616c7851c0bea14d4546f47e5`](https://github.com/Ryujoxys/sushiro-overdose/commit/e273df046789773616c7851c0bea14d4546f47e5)。具体字段见 [`internal/app/queue_live.go`](https://github.com/Ryujoxys/sushiro-overdose/blob/e273df046789773616c7851c0bea14d4546f47e5/internal/app/queue_live.go#L37)，封装兼容线索见 [`collector/collector/sushiro_client.py`](https://github.com/Ryujoxys/sushiro-overdose/blob/e273df046789773616c7851c0bea14d4546f47e5/collector/collector/sushiro_client.py#L74)。

- 单店详情读取 groupQueues 中 booth、mixed、counter、reservation 分队列数组，以及 groupQueuesCount 等线索。数组不是完整可处理队列的证明。
- 列表与详情能力不同，列表快照不能取代叫号数组；现有采集器文档的默认 15 分钟采样不能满足我们的近时段监控。
- 现有实现用展示最大号码做进度推断，本项目必须先核实该推断成立条件。
- 查询缓存、旧数据回退和有限字段都需要显式处理。
- 公开源码中的共享鉴权值不在本项目保存、传播或使用。2026-10-02本项目对旧目录的真实无凭证GET为HTTP401；此处为当时研究结论。后续新gateway的有效上下文目录、三店和短窗已验证，当前事实详见DATA_ACCESS.md与E0036/E0048，不以旧地址失败代替新地址验证。

## 4. 接入验证清单与人工配合

首个开发里程碑是少量大陆门店的只读对照验证：

1. 确认正常请求的数据来源、适用使用范围和访问方式；查询与个人权限独立管理。
2. 将堂食/预约展示三位、等待桌数逐项对应到响应字段，保存字段是否缺失、单位、顺序及队列类型。
3. 核实是否有源更新时间、完整叫号游标、最新放号或队尾指标；缺失就保留未知，不虚构数值。
4. 在允许频率内比较 60 秒、30 秒刷新，测量源缓存、延迟、失败和限流；共享同店请求。
5. 使用用户正常已有号单和现场观察补充真实叫号、过号与入座结果；不为测试制造取号和取消流量。
6. 记录事实、置信范围和失败样本，再决定数据结构及模型输入。

投屏可以验证画面含义，不能仅凭画面得到请求地址、签名或业务登录态。人工配合包括正常登录、现场核对、确认门店规则；原始网络记录可能含秘密，必须在分享、保存或提交前清理。

## 5. 官方资料与边界

- [Monstarlab 大陆寿司郎小程序案例](https://www.monstar-lab.com.cn/cases/sushiro/) 支持官方产品存在远程取号、预约等业务的判断；未提供本项目可以直接使用的公开第三方 API 合约。
- [腾讯关于 openid 的说明](https://cloud.tencent.com/document/product/1301/73438)：应用身份与 appid 相关，用户登录我们的应用不等于取得寿司郎应用业务身份。
- [腾讯小程序订阅消息说明](https://cloud.tencent.com/document/product/1301/103770)：订阅授权与实际发送能力需要单独核验，不能默认可无限推送。

这些资料用于划分设计条件；上线、接口许可、运营规则和消息额度须在相关阶段核实。

2026-10-02 后续正常小程序捕获及独立只读请求确认，西单大悦城店实际使用 `https://sapi.sushiro.com.cn/gateway/wechat/api/2.0/getStoreById?storeId=3004`，返回 HTTP 200、直接 Store 对象与四类展示数组。这项本机证据补充了公开参考源码使用旧主机的局限；旧目录的匿名 401 与新主机单店成功分别记录。2026-10-02当时新接口目录地址、凭证续期、字段单位与持续刷新未验收；2026-10-03已补充固定目录147条、三店短窗及新正常凭证导入后的查询实证，正常初始化来源已确认、独立自动更新仍未实现，见E0036/E0048/E0049。短窗结果不能推全国可用、字段单位或源新鲜度。实际适配与后续结果见 DATA_ACCESS.md 和交接文档 E0017 起的记录；没有模型或部署验收结果。


## 6. 2026-10-04 更新来源与辅助端有限研究

本轮锁定下列公开提交，只读所列文件，不运行项目、不复制源码/共享鉴权或测试公开凭证。结论限已读范围，“自动”名称、README和缺搜索结果均不证明实际独立续期。

| 仓库及提交 | 本轮实际范围 | 可借鉴与限制 |
| --- | --- | --- |
| [Ryujoxys/sushiro-overdose a2514e3](https://github.com/Ryujoxys/sushiro-overdose/tree/a2514e3c5044c1b9ab8516e37fd63ca2c5e99af4) | internal/app/auth_lifecycle.go、auth_health.go、tokens.go；internal/api/api.go | 已保存查询上下文/到期状态/捕获管理；所读API使用旧crm，没有本项目可验收的正常SAPI新code/initialize供应 |
| [anran11-pku/sushiro-smart 8fa4f16](https://github.com/anran11-pku/sushiro-smart/blob/8fa4f16511e1ccc9a9ca4b9e6bbfdd8d41656efa/internal/api/api.go) | internal/api/api.go，MIT元数据 | 旧crm查询；所读文件无实际SAPI正常初始化生成，不能推整个项目/分支不存在 |
| [MiliJhM/sushiro-smart-mili 2118f72](https://github.com/MiliJhM/sushiro-smart-mili/blob/2118f725e6cb913f7d2bd27e3a522b5f3945b4eb/internal/api/api.go) | internal/api/api.go，MIT元数据 | 同类查询/操作隔离线索；没有取得独立新code供应证据 |
| [donokey/sushiro-monitor ada85d0](https://github.com/donokey/sushiro-monitor/tree/ada85d023b276f56da995c4e1783b8b9de0c71f9) | monitor.py、capture_proxy.py，license元数据空 | 捕获/配置查询方式；不当作独立初始化已验证，不复制无许可实现 |
| [Gitnapp/sushiro-skill b5760cc](https://github.com/Gitnapp/sushiro-skill/tree/b5760cc286f7c95a881730ab428a3e6b1c439436) | README.md、scripts/sushiro，MIT元数据 | 旧crm静态查询方式；未落实本项目独立SAPI更新 |

[keliguru/sushiro-queue fb3725e](https://github.com/keliguru/sushiro-queue/tree/fb3725ea83482b5661f97cd7e32de046f6259c19)仅查看树/README线索，不计为实现审查。

[f1tz/wxunpack cdedad1](https://github.com/f1tz/wxunpack/tree/cdedad11c3e8dff7e72338e2b491e3db62439459)提供目标客户端包格式研究线索；自行有界解析本人正常下载的明确目标181包，官方源码只在内存解析而未执行/发布。[Tencent/MMKV MiniPBCoder.cpp ad7657e](https://github.com/Tencent/MMKV/blob/ad7657ef9d120dbcdd7432d75aa6c59391149b22/Core/MiniPBCoder.cpp)用于有限格式比较；目标三份存储未按该格式解析出新凭证，不推断一定加密、未猜密钥或读取聊天。

目标181客户端正常login.code→js_code、固定启动long_token及标量排序HMAC-SHA256与本人请求离线一致；官方静态密钥不用于本项目实现，新的运行时code供应仍缺方案。研究中的范围纠正、摘要遮蔽失败与处置完整记录E0052；不输出/公开原文或静态值。

原创同电脑适配器按Surge官方 [响应hook](https://manual.nssurge.com/scripting/http-response.html)、[脚本参数](https://manual.nssurge.com/scripting/overview.html)、[HTTP客户端](https://manual.nssurge.com/scripting/api.html)设计。支持条件、仅回环/白名单字段、60秒参数、DIRECT/禁重定向Cookie与流量视图记录限制见[BRIDGE.md](BRIDGE.md)。合成联动已通过，真实Surge/微信和跨到期仍未验收，不能代替独立服务器续期。


## 7. 2026-10-04 正常登录资料与日期证据复核

尝试读取微信wx.login/登录流程官方页面时浏览工具不能打开，未据此补写新协议结论。腾讯官方[OneID微信小程序登录文档](https://cloud.tencent.com/document/product/1441/68677)可读取，说明其code来自客户端wx.login；该文档属于OneID认证服务，不是寿司郎的更新接口，不能用于声称本项目有新的SAPI供应端。已有微信api-typings固定提交与目标181证据仍以AUTH_REFRESH为准；未调用相关POST。

为日期特征准备核对2026国务院通知；中国政府网原页面403、繁体镜像重定向循环，改读[北京市政府转载正文](https://www.beijing.gov.cn/fuwu/bmfw/sy/jrts/202511/t20251104_4258838.html)，来源标中国政府网、发布日期2025-11-04。当前两日样本落在10月1–7日国庆假期，10月10日周六安排上班；日期类型不能仅按星期判断。尚未实现/存档全年日历，也不能从该通知推门店营业保证或客流。数据盘点和后续真实标签/泄漏防范契约见[DATASET_DESIGN.md](DATASET_DESIGN.md)。

## 8. 2026-10-04 开发者工具与H5路线的前提核对

用户提供两份建议截图，希望改善独立小程序窗口读取；截图是待核对材料，“100%透明/识别”不作为能力证据。微信三页官方自动化快速入门、真机调试、web-view正文均无法打开；[官方发布的miniprogram-automator包](https://www.npmjs.com/package/miniprogram-automator)搜索结果可读到launch的cliPath/projectPath示例，直接正文403，未安装/执行该包。仅支持自动化针对开发者工具项目的事实，不能证明空项目可接管任意已运行的第三方小程序。微信公众号登录、本项目appid或空目录都不能自动赋予寿司郎项目/正常应用登录上下文。

[微信团队miniprogram-ci说明](https://github.com/wechat-miniprogram/miniprogram-ci-dist)正文核对：编译/预览使用项目代码，预览/上传需相应管理员取得的代码上传密钥等条件；该工具与真机调试/自动化的权限不是同一件事，不能把其要求泛化为所有调试操作。没有证据支持截图中开发者工具直接连接寿司郎发布版的路径，未开启隐藏调试、注入/修改微信或关闭平台校验。

H5路线只在确实存在排队网页且普通浏览器能使用它时成立。对5份本人已明确提供的私有捕获仅按既有sapi域名检查URL路径/响应MIME的类型统计，共24条、全部application/json，其中固定详情路径6条，HTML MIME与.html/.htm路径均0；没有输出URL/查询参数/头/正文或其他域名，新增网络0。该短窗且限域证据只表示尚未发现H5入口，不证明全部页面/其他域名不存在网页；JSON API地址不是已验证的H5交互页。是否依赖微信登录/JS环境需真实普通浏览器检验，不能保证复制链接即可运行。

建议先有限核对H5前提，开发者工具保留为后续本项目自有小程序的开发/测试工具；当前服务器主线仍以已确认只读API、正常新查询上下文提供方与真实跨到期验收为核心。能看见界面和能够全天自动取得新上下文分别验收，不将前者替代后者。上节年度日历“尚未实现”为其早期研究状态；随后rc3已公开2026日历资源和离线模块，实际结果见CALENDAR与E0068/E0069，旧研究原文保留。

## 9. 2026-10-04 电脑控制权限与会话排查

实际读取[OpenAI Computer Use官方说明](https://learn.chatgpt.com/docs/computer-use)及[故障排查说明](https://learn.chatgpt.com/docs/reference/troubleshooting)。前者区分系统录屏/辅助功能与应用访问授权，建议不能查看或操作时核对系统权限；后者列出日志位置并要求分享前检查敏感信息。本轮没有读取其他会话、整批日志或提交反馈。

本机系统设置中Codex Computer Use的辅助功能和录屏与系统录音开关实际均为on，设置页面可以正常读取文字及截图；只读核对，没有更改权限。重置工具会话再按小程序实际bundle绑定仍timeoutReached，未重启微信、修改运行时或新开网络调试。结果进一步排除两个开关处于off的解释，未确定微信窗口读取原因，不证明用户页面/API失败或电脑采集已接通。微信官方限定域名检索此次无结果，不以搜索缺失新增协议结论。

## 2026-10-06 小助手正常客户端补充

正常下载v23的静态结构及本项目独立查询见交接E0213–E0220和[ANONYMOUS_QUEUES.md](ANONYMOUS_QUEUES.md)。取数路径直接以storeid调用两条官方CRM匿名查询；本项目原创客户端在不发送微信授权、Cookie或冒用应用身份时，三试点已有200及有界连续结果。两家官方正常UI近时号码相容，但数量含义、同次身份关联和源刷新时间仍未认证。

检查到的小助手估时路径从内置41组数对拟合固定一元线性关系，按数量取整；未见该路径使用日期/时段、门店或LLM输入。来源/误差未知，不能等同本项目要求的预测验收。只总结方法事实，未公开第三方实现、内置数对、系数、真实原始样本或个人资料，不能据此扩大接口使用范围或复制无许可证代码。


## 2026-10-07 个人票据线索的重新验证

本轮重读此前锁定提交的[sushiro-cli状态查询](https://github.com/lmxx1234567/sushiro-cli/blob/982bebfb92fb57f017f9f27878cfd467f1da0410/internal/api/client.go)：旧查询带wechatId，要求netTicket/reservationTicket字段；[Ryujoxys接口实现](https://github.com/Ryujoxys/sushiro-overdose/blob/e273df046789773616c7851c0bea14d4546f47e5/internal/api/api.go)提供个人状态和取号/取消线索。后者有实现及作者的抓包声明，不是本项目端到端成绩；未复制实现或使用共享凭证。

本人正常当前小程序的实际状态GET没有wechatId参数，另有statusHistory按ticketId查询；本项目核本人UI与返回一致后，各一次独立GET全200。返回的票据等待值、按桌型数量及状态历史值得研究，尚未证实精确个人排名或新取号队尾。历史本次只有WAITING，无时区时间戳，不能作为已叫号标签。用户最新明确授权本人自动取号正常测试，更新前文旧未授权限制；现有号单保留、没有新出票或取消。当前协议与接入范围见[PERSONAL_QUEUE_ACCESS](PERSONAL_QUEUE_ACCESS.md)、R29和E0280，不按旧地址猜写入。
