
# 短时本机接入与采集恢复

rc5接入诊断候选；原dev5接收能力保留。完整需求、研究、检查及限制见 [PROJECT_HANDOVER.md](PROJECT_HANDOVER.md) E0052–E0053；正常授权证据见 [AUTH_REFRESH.md](AUTH_REFRESH.md)，既有查询命令见 [DATA_ACCESS.md](DATA_ACCESS.md)。

**2026-10-04电脑微信正常刷新→Surge→回环接收器→私有revision4提交已实际成功一次。** 没有用这份新上下文执行独立GET；重复可靠性和连续跨到期仍待验收。2026-10-05正常刷新重复窗口超时，安全诊断有效投递0，当前revision4已过期。这个工具接收正常查询上下文，不自动登录、刷新小程序或续期。

## 适用环境与数据流

仅同一台电脑：正常微信客户端发出固定sapi门店详情GET，Surge响应脚本将该次成功请求的六项查询头和公共门店id/name转发到127.0.0.1随机端口。iPhone脚本中的127.0.0.1指手机，不能接入电脑；当前没有LAN/云端版本。

Python运行只用标准库。脚本参考 [Surge响应脚本](https://manual.nssurge.com/scripting/http-response.html)、[脚本参数及本地路径](https://manual.nssurge.com/scripting/overview.html) 与 [HTTP客户端API](https://manual.nssurge.com/scripting/api.html)。DIRECT、禁重定向/自动Cookie要求Surge Mac 5.5+。Node只用于适配器回归检查，不是Python运行依赖。

要求现有完整gateway私有文件、该文件referer中的目标小程序身份、明确1–3店、递增revision；初次没有文件仍须按DATA_ACCESS从本人明确HAR导入。接收整组来自同一成功单店响应，不把新授权与旧头混合。

## 开启一次接收窗口

下列变量由本机操作者设置，路径不贴聊天/提交仓库。直接父目录须当前用户0700，现有上下文0600；session是未存在的私有文件名，与上下文不同。当前私有文件已为过期revision4，下一次实际更新须revision5或更高，且大于文件当前revision；示例不能代替运行时重读和到期检查。

```sh
PYTHONPATH=src python3 -m sushiwait context-bridge   --credentials-file "$SUSHIWAIT_CONTEXT_FILE"   --session-file "$SUSHIWAIT_SESSION_FILE"   --revision 5 --store-id 3004 --seconds 60 --diagnostics
```

命令发出bridge_ready后，私有session文件含本轮端口、随机授权、截止时刻、应用身份/门店范围及URI编码adapter_argument。只在同电脑受控配置中读取adapter_argument，不打印/截图/发送其值。文件在成功、超时或中断后清理，过期参数不能复用。

Surge临时响应规则模板如下；LOCAL_SCRIPT_PATH和PRIVATE_ARGUMENT须替换为本机脚本绝对路径及当前私有参数，不把替换后的规则公开：

```ini
[Script]
sushiwait-context = type=http-response,pattern=^https://sapi\.sushiro\.com\.cn/gateway/wechat/api/2\.0/getStoreById\?storeId=[1-9][0-9]*$,requires-body=true,max-size=262144,script-path=<LOCAL_SCRIPT_PATH>,argument=<PRIVATE_ARGUMENT>,timeout=3,debug=false
```

脚本为 [adapters/surge-context.js](../adapters/surge-context.js)。仅沿用已授权sapi单域名临时HTTPS调试，不增加其他域名，不新开原始HAR/数据包捕获。观察一次正常门店刷新，随后及时关闭临时解密/相关捕获并移除或停用临时规则。固定西单3004规则曾在真实Surge成功；不能据此保证每次电脑微信流量进入规则。实测使用私有脚本内存装载参数，未将argument粘贴进可见工具/界面。

**接收器最多60秒和脚本参数到期只限制接收/转发，不能自动关闭Surge MitM/VPN/捕获。** 实际调试开关必须另行及时关闭。Surge官方说明脚本网络请求会出现在流量视图，因此本工具“无请求原文日志”不代表Surge没有记录；不要导出或分享包含私有参数的记录。

## 接收与退出结果

- 只监听IPv4 127.0.0.1随机端口；要求本轮随机Bearer、准确Host，无Origin/Transfer-Encoding/Expect，POST JSON不超过32KiB。
- 仅固定成功门店GET、对应app、指定店、请求/响应身份匹配；白名单authorization/x-app-client/x-app-code/user-agent/referer/content-type，响应只留id/name。其他私人响应字段不进入接收器或数据库。
- 相同授权拒绝；递增revision、声明有效且剩余超过30秒才可原子提交。未知声明沿用既有未知处理，不推默认有效期。
- 成功exit0，输出committed/durability_confirmed和清理状态；server_acceptance=unverified/network_verified=false。network_performed=true仅因本机回环，external_network_performed=false；relay_received_at是本机接收时刻，不是上游查询或源时间。
- 超时/错误exit1；CLI中断exit130。关闭端口并清理仍是原文件的session；若被其他同用户进程替换则保留且session_cleanup_confirmed=false。无需重复覆盖不明文件，先核对安全状态；提交后持久化失败仍明确committed，不盲目回退。

接收器不联系SAPI、不取新code/签初始化、不操作微信/Surge、不创建个人号码。临时端口和私有参数只给本机明确操作者，不能用于浏览器跨站调用或任意URL转发。

## 当前电脑准备状态

2026-10-04实际环境为macOS26.6.2、微信4.1.13、Surge Mac6.4.3，既有CA已受系统信任。正常西单刷新于北京时间22:05:47.212完成revision4原子提交和持久化确认，session清理、私有脚本复位通过；声明签发22:01:54、到期23:01:54/3600秒，未验签，不推固定TTL。用户随后关机，此上下文独立GET未执行。2026-10-05声明已过期，下一次须5+；首次用户没赶上窗口，后两次报告正常刷新仍超时，其中安全诊断有效投递0。所有临时规则已移除并保存，原有crm域名恢复，MitM/原始捕获关闭；原代理/增强模式、LAN/API/CA权限没有改动。

[Surge模块文档](https://manual.nssurge.com/profile/module.html)说明本地sgmodule可覆盖hostname并添加Script，但不能改变CA或替代MitM主开关；私有参数应由本机文件生成/装载，不经聊天或可见工具参数粘贴。[CLI文档](https://manual.nssurge.com/tools/cli.html)标明扩展module/feature控制需Mac6.8.0+；当前6.4.3的实际help没有这些命令。不能直接按新版示例控制解密。旧CLI的environment可读取，但set MitMEnabled false实际返回Illegal parameter；主开关已用正常UI按时关闭并核对，不能称CLI自动关闭已实现。此受控窗口不是无人值守看门狗；未启用HTTP API、局域网访问或常开捕获。最后一次重复试验临时开启脚本debug仅为每次重读私有文件，原适配器没有console日志，结束后规则移除。

## 可选安全诊断（rc5）

`--diagnostics`在成功或等待超时结果中加入固定统计：local_connections是本机接受的TCP连接数，authenticated_deliveries通过本轮token/Host/路径与来源校验，validated_observations通过完整观测解析，rejected_observations及last_rejection表示观测/提交拒绝。格式错误在观测阶段前停止，不计入观测拒绝；连接、鉴权和有效观测不应混为一项。没有接收窗口的输入错误不编造统计；默认输出保持。

local_connections=0只说明本轮未接到连接，不能确定是没有正常查询、流量未接管、规则未匹配或适配器提前退出。authenticated_deliveries=0也可能有被拒绝的连接。统计不输出地址/端口、原文/头、应用身份或凭证，不另写日志，不延长60秒窗口，也不取得新授权。

## 采集等待正常新上下文

可以在另一个本机终端运行：

```sh
PYTHONPATH=src python3 -m sushiwait collect   --api-profile miniapp_gateway --credentials-file "$SUSHIWAIT_CONTEXT_FILE"   --store-id 3004 --interval 30 --samples 3   --wait-for-credentials 300 --db "$SUSHIWAIT_PILOT_DB"
```

默认等待0秒保持原来的首错停止。显式1–600秒仅私有文件模式可用，采样边界仍为1–3店/30–3600秒/1–120轮。

只有声明过期/临近到期才进入暂停：保存一次安全preflight事件、输出credentials_paused，随后逐秒只读取本机文件，不发查询、不写每秒失败记录。必须有严格更高revision、不同授权且当前保护通过；恢复输出credentials_resumed，用时再次检查。坏文件/权限/倒退/无效声明立即失败，等待超时输出credentials_wait_timeout。HTTP401、限流、TLS、网络/字段错误不进入等待、不自动重试。

恢复后重置采样时间基准；若在周期等待中暂停，恢复后等待完整周期，避免补发追赶请求。它不产生新授权、不做守护进程或跨重启任务恢复；协作提供方必须及时写入实际正常完整新上下文。

实际Surge Mac6.4.3的CLI script evaluate已执行自写合成脚本，DIRECT向独立本机回环服务器投递成功、HTTP200标记确认，未用真实凭证/外部请求或修改配置。只证明Surge脚本的本机HTTP能力可用，不证明真实SAPI响应规则被触发。

## 检查与真实验收

原dev5最终252项Python检查/1.680秒通过，其中wrapper执行26个JS场景，合成回环端口与模拟上游验证同进程提交/恢复、默认停止、权限/超时/错误和节奏。没有跳过Node检查；有Node的环境可单独执行：

```sh
node tests/surge_context.test.js
```

rc5完整324项/4.111秒通过，既有26个JS场景计入Python wrapper；新增3项真实合成回环及4项CLI检查覆盖无投递、旧/到期授权、安全计数、默认输出及错误分发。回环绑定须执行环境允许本机端口；bridge_unavailable不代表SAPI失败。真实revision4接收侧已成功一次，当前过期；主库219成功2失败、真实恢复未验收。后续重复窗口未收到有效投递，CLI近期摘要限定目标域名匹配0，缺完整客户端请求证据。先验证正常流量/规则与新上下文，再做固定GET服务端接受和同进程跨到期；服务器独立code供应与无人值守更新未解决，不把本工具当全天服务。
