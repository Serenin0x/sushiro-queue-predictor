# 无 HAR 的电脑正常查询接入

rc6新增 `context-surge`。实际研究、安装和发布状态见 [PROJECT_HANDOVER.md](PROJECT_HANDOVER.md) E0084起；原响应脚本路径仍见 [BRIDGE.md](BRIDGE.md)。这是辅助端接入工具，不是服务器独立登录或全天自动续期。

## 为什么增加这条路径

2026-10-06凌晨，电脑微信画面已能读取。正常关闭小程序后，在仅sapi短窗中重新打开：Surge实际看到了正常初始化、目录等请求；西单闭店、列表没有“查看排队情况”按钮，没有观察到原响应规则要求的getStoreById。本轮接收器TCP连接0，不能用它解释此前营业窗口的全部失败。

正常目录查询也提供完整六项查询上下文。另一次短窗在查询发生时读取Surge已有内存摘要，未导出HAR、未开启原始磁盘捕获。新授权与旧revision4不同、目标应用一致、声明剩余超过30秒；小程序和临时解密关闭后，电脑用该完整上下文成功查询三店，随后安全原子更新为revision5。这是临时研究辅助程序的真实结果；修正版正式命令后来接入新6并真实恢复原西单进程，独立证据见E0090，不能把早先研究程序当命令验收。

## 环境与运行

仅支持已观察的Surge Mac6.4.3本机CLI摘要格式。Python标准库，无新运行依赖；固定调用已安装的 `/Applications/Surge.app/Contents/Applications/surge-cli dump request --raw`，不支持任意命令或可配置程序地址。其他平台或格式明确拒绝；安装帮助存在不意味着这台设备实际装有Surge。

已有gateway私有文件必须包含完整上下文和正确应用referer，父目录0700、文件0600/0400，revision显式递增。当前研究文件已提交revision6；运行时仍须重读当前版本，示例数字不是默认版本。

```sh
PYTHONPATH=src python3 -m sushiwait context-surge \
  --credentials-file "$SUSHIWAIT_CONTEXT_FILE" --revision 7 --seconds 45
```

收到 `surge_ready` 后，操作者在已授权的sapi单域名短窗内正常重新打开小程序，进入“用餐预约”门店列表。命令不打开微信、不启用解密、不刷新页面；结束后须及时关闭临时解密并恢复原名单。不要扩展域名、开启原始捕获或分享摘要原文。

命令最多观察60秒，约每秒读取一次本机摘要，没有SAPI请求或回环监听。只读取 `recent-requests`，忽略active和其他容器；近期记录最多200条，原始stdout最大4MiB，仅在内存，stderr不记录，子进程超时/超限终止并回收，不保存原始摘要。

## 选择和写入规则

- 只接受固定HTTPS主机和 `/gateway/wechat/api/2.0/stores` GET，三个目录参数latitude=1、longitude=1、numresults=10000；重复/额外参数、其他主机/端口、片段和控制字符拒绝。行内请求目标还须与目录地址一致，不从初始化、账号或其他请求取得凭证。
- 只接受窗口开始后完成、completed=true、failed=false、没有请求body的HTTP200记录。completedDate实测为Unix秒；兼容离线Mac2001参考表示，只有唯一时间落在当前≤60秒窗才接受，未知/未来/窗口前时间拒绝。早期纪元推定及50条上限已由真实CLI纠正，见E0089。它是查询响应时间，不是叫号源更新时间。
- 六项查询头来自同一记录，严格检查缺项、重复、续行、完整上下文、应用身份、新授权和声明保护。Cookie、notes、响应头其他字段、其他请求不进入结果或私有配置；不同记录不拼接。声明未知或保护停止的候选拒绝，不猜期限。
- 选择最新完整候选；相同时间的不同上下文拒绝。观察中当前文件被其他写入者改变就停止；提交复用已有私有目录锁、revision校验、原子替换和持久化确认，到期提交保护再次检查，不覆盖更高revision。

成功输出仅安全来源/时间、revision、committed、durability_confirmed、声明时间及验证状态。`observed_http_status=200`是正常目录摘要的状态；`response_body_inspected=false`，没有校验目录body或门店身份，也没有验证本次独立查询。`server_acceptance=unverified`、`network_verified=false`必须保留。不能把保存配置称为稳定续期。

失败输出固定错误码，保留旧文件。没有正常新请求会超时，只有沿用旧授权则输出unchanged；未知格式、超限、权限、声明或应用不符立即停止，不开启自动重试上游。CLI成功/超时均不会关闭Surge开关。

## 接收后独立验证

关闭小程序和临时调试后，按 [DATA_ACCESS.md](DATA_ACCESS.md) 使用系统信任CA、完整TLS和固定profile，先做1–3店有界只读查询。独立成功、持续刷新、同进程跨到期、正常新凭证提供方及服务器运行分别验收。

本轮正常目录声明01:07:18–02:07:18，随后01:18:51–01:18:52三店独立成功、耗时210/189/307ms，均CLOSED/签到聚合0、源新鲜度unknown。这证明闭店时可以查询结构化状态，不证明营业期叫号变化、零等待或全国覆盖。旧主库219成功2失败保持，新增样本保存在另一明确的私有试验库；不得混写统计。

## 检查

当前21项新增离线检查覆盖来源/请求行/窗口/纪元/完整头/应用/到期/候选冲突、私有写入和并发保护、CLI与有界子进程回收。当前全345项/4.246秒通过，无跳过，既有26个JS场景已含在wrapper，不另计为371项。安装、GitHub及实际接入以编辑历史后续实证为准。没有真实标签、ETA、业务取号或部署结果。

rc6最终本机安装检查：16个代码模块、2026年度JSON及构建时README元数据逐字节一致，运行依赖0；checkout外隔离安装版本rc6，context-surge帮助/三个必需选项和既有诊断选项通过，公共/结果/日期/信号四路径socket0、真实训练标签0。wheel与实际公开/CI审计见E0086及后续历史；不当真实接口或独立续期验收。

rc6实际公开代码[0c4cce8](https://github.com/Serenin0x/sushiro-queue-predictor/commit/0c4cce8f0ae64aa9aed39dcc80be1fcdb951819d)、137远端blob一致；[CI运行37351645572](https://github.com/Serenin0x/sushiro-queue-predictor/actions/runs/37351645572)三版本各343项/构建/checkout外安装全部success，context-surge帮助和四路径socket0通过，见E0087。这不等于正式命令真实接入、原进程恢复或全天服务器验收。


最新真实接入/恢复：5到期前保护暂停，45秒正常目录短窗于02:15:28.859209接入6，原进程02:15:59.320查询成功且继续采集；6声明02:10:55–03:10:55，下一正常7+。最后解密实际45秒后关闭/原名单恢复，随后小程序关闭，另两店02:18独立成功。此为一次受控客户端辅助恢复，服务器独立供应/长期可靠性仍未验；旧343项CI只属于0c4cce8，修正版另记。

修正版rc6最终本机wheel/16模块/年度JSON/当前README及checkout外安装已验，四路径socket0/context-surge帮助通过，SHA/实际边界见E0091；新345项远端CI与公开结果另记，不借初版343项。

修正版实际代码[2a0050e](https://github.com/Serenin0x/sushiro-queue-predictor/commit/2a0050edc1b482b3c69936f2025754f692393fe4)、137远端blob一致；[运行37355621524](https://github.com/Serenin0x/sushiro-queue-predictor/actions/runs/37355621524)三版本各345项/构建/checkout外安装全部success，帮助与四路径socket0通过，见E0092。真实一次恢复另见E0090；这仍不代表长期服务器/完整产品验收。

10-06有界运行最终完成：西单原进程120成功/exit0、90保护前+30恢复后，新库125成功1preflight，旧库219/2保持；只读质量报告socket0、数据库SHA前后一致，见E0093。新增[长期采集方案](LONG_TERM_COLLECTION.md)明确实际依赖和未实现组件，文档编辑未重复功能测试或重建wheel。
