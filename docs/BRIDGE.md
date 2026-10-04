
# 短时本机接入与采集恢复

dev5开发工具。完整需求、研究、检查及限制见 [PROJECT_HANDOVER.md](PROJECT_HANDOVER.md) E0052–E0053；正常授权证据见 [AUTH_REFRESH.md](AUTH_REFRESH.md)，既有查询命令见 [DATA_ACCESS.md](DATA_ACCESS.md)。

**已实现并通过合成联动；真实微信→Surge→接收器及连续跨到期尚未验收。** 本轮未安装真实Surge配置、开启捕获或取得新授权。这个工具接收正常查询上下文，不自动登录、刷新小程序或续期。

## 适用环境与数据流

仅同一台电脑：正常微信客户端发出固定sapi门店详情GET，Surge响应脚本将该次成功请求的六项查询头和公共门店id/name转发到127.0.0.1随机端口。iPhone脚本中的127.0.0.1指手机，不能接入电脑；当前没有LAN/云端版本。

Python运行只用标准库。脚本参考 [Surge响应脚本](https://manual.nssurge.com/scripting/http-response.html)、[脚本参数及本地路径](https://manual.nssurge.com/scripting/overview.html) 与 [HTTP客户端API](https://manual.nssurge.com/scripting/api.html)。DIRECT、禁重定向/自动Cookie要求Surge Mac 5.5+。Node只用于适配器回归检查，不是Python运行依赖。

要求现有完整gateway私有文件、该文件referer中的目标小程序身份、明确1–3店、递增revision；初次没有文件仍须按DATA_ACCESS从本人明确HAR导入。接收整组来自同一成功单店响应，不把新授权与旧头混合。

## 开启一次接收窗口

下列变量由本机操作者设置，路径不贴聊天/提交仓库。直接父目录须当前用户0700，现有上下文0600；session是未存在的私有文件名，与上下文不同。下一次实际更新须revision3或更高，且大于文件当前revision。

```sh
PYTHONPATH=src python3 -m sushiwait context-bridge   --credentials-file "$SUSHIWAIT_CONTEXT_FILE"   --session-file "$SUSHIWAIT_SESSION_FILE"   --revision 3 --store-id 3004 --seconds 60
```

命令发出bridge_ready后，私有session文件含本轮端口、随机授权、截止时刻、应用身份/门店范围及URI编码adapter_argument。只在同电脑受控配置中读取adapter_argument，不打印/截图/发送其值。文件在成功、超时或中断后清理，过期参数不能复用。

Surge临时响应规则模板如下；LOCAL_SCRIPT_PATH和PRIVATE_ARGUMENT须替换为本机脚本绝对路径及当前私有参数，不把替换后的规则公开：

```ini
[Script]
sushiwait-context = type=http-response,pattern=^https://sapi\.sushiro\.com\.cn/gateway/wechat/api/2\.0/getStoreById\?storeId=[1-9][0-9]*$,requires-body=true,max-size=262144,script-path=<LOCAL_SCRIPT_PATH>,argument=<PRIVATE_ARGUMENT>,timeout=3,debug=false
```

脚本为 [adapters/surge-context.js](../adapters/surge-context.js)。仅沿用已授权sapi单域名临时HTTPS调试，不增加其他域名，不新开原始HAR/数据包捕获。观察一次正常门店刷新，随后及时关闭临时解密/相关捕获并移除或停用临时规则。该模板尚未在真实Surge验收，不保证当前电脑微信流量一定进入Surge。

**接收器最多60秒和脚本参数到期只限制接收/转发，不能自动关闭Surge MitM/VPN/捕获。** 实际调试开关必须另行及时关闭。Surge官方说明脚本网络请求会出现在流量视图，因此本工具“无请求原文日志”不代表Surge没有记录；不要导出或分享包含私有参数的记录。

## 接收与退出结果

- 只监听IPv4 127.0.0.1随机端口；要求本轮随机Bearer、准确Host，无Origin/Transfer-Encoding/Expect，POST JSON不超过32KiB。
- 仅固定成功门店GET、对应app、指定店、请求/响应身份匹配；白名单authorization/x-app-client/x-app-code/user-agent/referer/content-type，响应只留id/name。其他私人响应字段不进入接收器或数据库。
- 相同授权拒绝；递增revision、声明有效且剩余超过30秒才可原子提交。未知声明沿用既有未知处理，不推默认有效期。
- 成功exit0，输出committed/durability_confirmed和清理状态；server_acceptance=unverified/network_verified=false。network_performed=true仅因本机回环，external_network_performed=false；relay_received_at是本机接收时刻，不是上游查询或源时间。
- 超时/错误exit1；CLI中断exit130。关闭端口并清理仍是原文件的session；若被其他同用户进程替换则保留且session_cleanup_confirmed=false。无需重复覆盖不明文件，先核对安全状态；提交后持久化失败仍明确committed，不盲目回退。

接收器不联系SAPI、不取新code/签初始化、不操作微信/Surge、不创建个人号码。临时端口和私有参数只给本机明确操作者，不能用于浏览器跨站调用或任意URL转发。

## 采集等待正常新上下文

可以在另一个本机终端运行：

```sh
PYTHONPATH=src python3 -m sushiwait collect   --api-profile miniapp_gateway --credentials-file "$SUSHIWAIT_CONTEXT_FILE"   --store-id 3004 --interval 30 --samples 3   --wait-for-credentials 300 --db "$SUSHIWAIT_PILOT_DB"
```

默认等待0秒保持原来的首错停止。显式1–600秒仅私有文件模式可用，采样边界仍为1–3店/30–3600秒/1–120轮。

只有声明过期/临近到期才进入暂停：保存一次安全preflight事件、输出credentials_paused，随后逐秒只读取本机文件，不发查询、不写每秒失败记录。必须有严格更高revision、不同授权且当前保护通过；恢复输出credentials_resumed，用时再次检查。坏文件/权限/倒退/无效声明立即失败，等待超时输出credentials_wait_timeout。HTTP401、限流、TLS、网络/字段错误不进入等待、不自动重试。

恢复后重置采样时间基准；若在周期等待中暂停，恢复后等待完整周期，避免补发追赶请求。它不产生新授权、不做守护进程或跨重启任务恢复；协作提供方必须及时写入实际正常完整新上下文。

## 检查与真实验收

最终252项Python检查/1.680秒通过，其中wrapper执行26个JS场景，合成回环端口与模拟上游验证同进程提交/恢复、默认停止、权限/超时/错误和节奏。没有跳过Node检查；有Node的环境可单独执行：

```sh
node tests/surge_context.test.js
```

回环绑定需要执行环境允许本机端口；普通沙箱可能报告bridge_unavailable，这不是SAPI失败。最新真实revision3已过期、主库219成功2失败；三店各67轮/201成功后保护暂停等600秒超时，没有revision4或真实恢复，不用合成记录标记live。用户已在电脑打开正常小程序，但工具读取独立窗口超时，不能据此认为流量已进入Surge或接收器。必须再实际验收正常客户端流量、完整新上下文提交、固定GET服务端接受、连续跨到期恢复与临时调试关闭，才能讨论长期辅助端。服务器独立新code供应仍未解决，不能把本工具当全天服务。
