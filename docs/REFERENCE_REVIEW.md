# 参考项目研究与可借鉴范围

研究日期：2026-10-02，Asia/Shanghai。本记录基于公开源码与作者文档的只读核对；没有安装或运行参考项目，没有调用寿司郎生产接口，也没有验证公开查询凭证是否有效。以下“源码存在”不代表接口目前可用，更不代表获得第三方服务承诺。

完整需求、版本状态和编辑记录以 [PROJECT_HANDOVER.md](PROJECT_HANDOVER.md) 为准。本项目的只读工具为原创实现，没有复制参考项目代码；下述研究与本项目实际接入验证分别记录。

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
- 公开源码中的共享鉴权值不在本项目保存、传播或使用。本项目已对目录作真实无凭证 GET 测试，得到 HTTP 401；授权后的数据能力仍未验证，详见 DATA_ACCESS.md。

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

这些资料用于划分设计条件；上线、接口许可、运营规则和消息额度须在相关阶段核实。已有工具离线测试与目录匿名连通测试，没有授权后接入、模型或部署的端到端验收结果。
