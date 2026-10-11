# 官方查询凭证自动供应：方案比较

更新：2026-10-11，Asia/Shanghai。本文是原创研究与实验计划。当前公开软件为候选 `0.2.0rc73`；新增本地YYB供应接口及无账号隔离运行结果见[实际试验](YYB_LOCAL_TRIAL.md)，尚未完成寿司郎兼容与无人供应。原云采集因 CRM HTTP403 持久停止。最新事实与操作历史以 [PROJECT_HANDOVER.md](PROJECT_HANDOVER.md) E0397起的后续记录为准。

E0414–E0419后续实测覆盖下文较早“未执行”的时点：本人已完成YYB首次授权，02:47首次及03:48一小时间隔后的目标取码通过，缓存时效元数据更新；原创组合接入43项模拟通过。正常初始化材料、寿司郎实际接受、业务到期恢复和云接线仍待。[初始化接入](SAPI_INITIALIZATION.md)记录实际边界，公开仍rc73，没有把协议供应成功称业务恢复。

## 结论与需要修正的表述

已证明：本人正常官方客户端产生完整查询上下文后，程序能够独立查询门店，且人工辅助更新后原采集进程曾跨声明到期恢复。未证明：没有本人点击时持续产生新上下文、纯服务器自动续期、官方全国连续采集。

“代理不会产生登录 code”仍成立。“任何服务器都必须常开图形小程序才能取得 code”不应作为普遍结论：本轮找到应用宝协议实现，它通过已授权协议会话请求指定 AppID 的登录 code，不依赖图形界面。源码中存在取码及会话续期路径；尚未执行、未验证寿司郎是否接受，也不是官方发布的第三方续期合约。

必须分别验证三个层次：微信／应用宝会话可用、目标小程序 code 可用、寿司郎完整查询上下文被详情接口接受。一个层次成功不代替后两个层次。应用宝的 refresh_token 与寿司郎样本中的 long_token 是不同系统的字段，不能互换。

## 已实际核对的实现

| 项目与固定资料 | 实际做法／可借鉴点 | 与本项目瓶颈的关系 |
| --- | --- | --- |
| [YYB-Go-Enhanced 协议说明](https://github.com/525815266/YYB-Go-Enhanced/blob/2cc8f1697d1753061c491f394cc4ebb50811725a/docs/protocol-api.md) | 扫码建立应用宝会话，`getCode` 接口指定目标 AppID；code 短期、一次性，同账号串行 | 值得优先核对的纯服务器候选；没有寿司郎成功证据 |
| [同项目取码实现](https://github.com/525815266/YYB-Go-Enhanced/blob/2cc8f1697d1753061c491f394cc4ebb50811725a/internal/protocol/pool.go) | 请求微信协议层，再返回解析得到的 code；空结果可重建会话后有限重试 | 不是只写了演示接口；具体账号和目标应用的接受仍要真实验证，不照搬重试策略 |
| [同项目会话更新](https://github.com/525815266/YYB-Go-Enhanced/blob/2cc8f1697d1753061c491f394cc4ebb50811725a/internal/protocol/loginbuffer.go)、[后台保活](https://github.com/525815266/YYB-Go-Enhanced/blob/2cc8f1697d1753061c491f394cc4ebb50811725a/internal/httpapi/keepalive.go) | 保存协议会话的真实更新字段，按到期检查、单账号互斥、失效停止 | 可以研究分层调度思想；这些字段不证明寿司郎也有长效刷新凭证 |
| [wxsign 部署资料](https://github.com/LeapYa/wxsign/blob/2c76536a763dc2fccbc76f389f8ddbf59a0a2aed/docs/DEPLOY.md) | 容器中保留 Linux 微信与目标小程序运行时，使用调试组件取得 code／存储态，再调用各品牌接口 | 说明“客户端放在服务器”是一条可研究路线；作者未提供寿司郎适配，本项目没有运行它 |
| [WechatOnCloud](https://github.com/Gloridust/WechatOnCloud) | Linux 微信配虚拟桌面和远程显示；容器管理与聊天运行分开 | 可研究正常客户端环境；它本身没有交付寿司郎续期器。不能从 README 推断我们的云主机已能运行寿司郎 |
| [sushiro-overdose 安全说明](https://github.com/Ryujoxys/sushiro-overdose/blob/a2514e3c5044c1b9ab8516e37fd63ca2c5e99af4/SECURITY.md)、[认证元数据](https://github.com/Ryujoxys/sushiro-overdose/blob/a2514e3c5044c1b9ab8516e37fd63ca2c5e99af4/internal/app/auth_meta.go) | 公共采集与本人取号分开；账号通过临时代理抓包连接，记录失效寿命并提醒重新捕获 | MIT 项目可参考生命周期隔离；未找到这些已读文件实现无人新凭证供应的证据 |
| [同项目历史采集客户端](https://github.com/Ryujoxys/sushiro-overdose/blob/a2514e3c5044c1b9ab8516e37fd63ca2c5e99af4/collector/collector/sushiro_client.py) | 旧采集客户端默认 CRM 地址，需要外部给定 token | 不能把它当作匿名403后的新官方来源或自动登录器 |
| [sushiro-cli](https://github.com/lmxx1234567/sushiro-cli)、[sushiro-hk-live](https://github.com/scmlewis/sushiro-hk-live) | 前者有门店查询；后者围绕香港 SUSHI-PASS、缓存和定时任务 | 查询／缓存可以参考；香港与大陆不同，也未提供当前大陆 SAPI 的自动登录来源 |
| [mitmproxy Addons](https://docs.mitmproxy.org/stable/addons/overview/)、[Appium UiAutomator2](https://github.com/appium/appium-uiautomator2-driver) | 前者能处理已有流量事件；后者能驱动 Android 正常页面 | 属于代理接收器或客户端操作工具，不能单独解决授权来源；Android 路线依赖常在线设备或经过验证的运行环境 |

YYB 固定提交为 `2cc8f1697d1753061c491f394cc4ebb50811725a`，2026-10-11 00:09:03 UTC+8；wxsign 固定提交为 `2c76536a763dc2fccbc76f389f8ddbf59a0a2aed`；sushiro-overdose 固定提交为 `a2514e3c5044c1b9ab8516e37fd63ca2c5e99af4`。时间表示代码提交时间，不表示线上寿司郎验证时间。

## E0419：直接代码搜索与认证实现补读

本轮通过GitHub代码搜索，精确SAPI域名结果0，`sushiro js_code`仅一个不相关比赛CSV，`sushiro X-App-Code`共11结果／读取前10元数据，指向已研究的sushiro-overdose与sushiro-cli。这比网页索引查询更直接，仍不证明所有公开／未索引项目都不存在实现；没有展开无关文件或反复换关键词。

补读同一固定sushiro-overdose提交的[手机捕获](https://github.com/Ryujoxys/sushiro-overdose/blob/a2514e3c5044c1b9ab8516e37fd63ca2c5e99af4/internal/app/mobile_auth_capture.go)、[字段提取](https://github.com/Ryujoxys/sushiro-overdose/blob/a2514e3c5044c1b9ab8516e37fd63ca2c5e99af4/internal/core/capture.go)、[认证生命周期](https://github.com/Ryujoxys/sushiro-overdose/blob/a2514e3c5044c1b9ab8516e37fd63ca2c5e99af4/internal/app/auth_lifecycle.go)及[失效提醒规范](https://github.com/Ryujoxys/sushiro-overdose/blob/a2514e3c5044c1b9ab8516e37fd63ca2c5e99af4/specs/006-auth-staleness-reminder/spec.md)：临时MITM从本人正常请求取字段，完整后仍需只读探测；生命周期guard防止旧任务在重置后提交，失效提示重新捕获，规范明确不做自动续期。没有在这些已读实现找到为我们生成正常初始化设备／版本／日期头及签名材料的供应器。代码默认CRM来源，不能拿其历史匿名说明证明今天匿名通道仍可用。

作者将复用凭证与多客户端会话冲突关联，是作者观察与推断；本项目未证明SAPI查询也遵循同样互斥规则，更不能用它解释原匿名CRM403。日后本机接受测试可观察会话变化，失败按实际阶段归因。参考的生命周期原则在本项目整组CAS／查询验证中已有对应限制，本轮不因看到相似设计新增重复实现。

本轮确认MIT许可文本，但没有复制／安装／运行该项目，没有采用其请求改写、扩域解密或20分钟局域网捕获，不改变现有认证／代理范围。主线仍是已获授权的YYB供应→已验证正常材料→一次SAPI初始化→明确店GET；私有源包范围被自动审批拒绝的检查未执行，具体问题仍待本人答。

## 依赖与发布边界

E0426限定资源路径复核：固定YYB提交的[协议说明](https://github.com/525815266/YYB-Go-Enhanced/blob/2cc8f1697d1753061c491f394cc4ebb50811725a/docs/protocol-api.md)列出取码、手机号、用户资料及operateWxData兼容转发，但没有在该文档找到目标小程序包的下载接口；通用转发需要真实payload，路由名不生成目标业务签名或参数。[resources.go](https://github.com/525815266/YYB-Go-Enhanced/blob/2cc8f1697d1753061c491f394cc4ebb50811725a/internal/httpapi/resources.go)恢复的是YYB自己的模板／静态网页素材，不是寿司郎包。结论只限这两个已读文件及树的路径元数据，不推断所有SDK／其他项目绝无方法。本轮没有尝试通用协议、手机号／密钥能力、新账号访问、官方下载或私有缓存；正常初始化材料缺口仍未补齐。

E0420补读已研究的sushiro-cli，同一固定`982bebfb92fb57f017f9f27878cfd467f1da0410`，未发现默认分支新提交。其[credentials.go](https://github.com/lmxx1234567/sushiro-cli/blob/982bebfb92fb57f017f9f27878cfd467f1da0410/internal/auth/credentials.go)明确只导入已有会话、不生成会话；[import.go](https://github.com/lmxx1234567/sushiro-cli/blob/982bebfb92fb57f017f9f27878cfd467f1da0410/internal/auth/import.go)的FromRequest被动提取一次正常请求的字段，不发网络、不混合不同会话；public_import也只处理已有交换文档。检查范围限这三个认证文件及limitations，不把已读范围推为整个项目的绝对不存在，也不把旧CRM认证导入器误当SAPI初始化材料供应器。本轮没有运行或复制该项目、使用其公共凭证或发出寿司郎请求。

wxsign 的 [LICENSE](https://github.com/LeapYa/wxsign/blob/2c76536a763dc2fccbc76f389f8ddbf59a0a2aed/LICENSE) 明确不是开源许可，禁止未经许可再分发；其原代码不并入本仓库。已读部署还依赖特定客户端／WMPF版本、Frida调试、放宽容器权限与设备信息处理。这些不是正常虚拟桌面的必需条件，本项目没有执行它们。其脚本扫描多个存储上下文、身份字段及关闭 TLS 校验的做法不采用。

YYB 本轮固定提交完整树没有发现 LICENSE／COPYING，仓库元数据未给出识别许可；上游 `SuperNaiBA/YYB_GO` 的本轮根目录也没有给出许可。WechatOnCloud 本轮根目录未发现许可文件，仓库元数据未给出识别许可。公开源码不等于可复制分发；在明确许可前不复制这些代码、不打包镜像、不把它们称为本项目已接入的开源依赖。sushiro-overdose 的仓库元数据识别为 MIT；如果以后实际复用，仍需保留对应许可和来源。

本轮没有安装或运行上述第三方项目，没有提交账号给这些项目或公共演示，也没有启用云代理、上传当前本机凭证或开放管理端口。YYB 的 [Compose](https://github.com/525815266/YYB-Go-Enhanced/blob/2cc8f1697d1753061c491f394cc4ebb50811725a/compose.yaml) 默认发布8000端口，不直接照搬到现有仅开放只读18080的服务器。作者的 Linux 运行资源估计不等于我们的4GB云主机实测容量。

## 推进顺序与有限实验

1. **协议路线先完成无账号审阅。** 已找到取码和协议会话更新实现；下一步核对许可、扫码授权的接收方／范围、目标 AppID 会话绑定、调用链与寿司郎完整上下文衔接。源码阅读可以继续；不先安装并索取扫码，更不把得到一个 code 当作寿司郎接入完成。涉及新软件和账号保存位置时，先给出具体可审阅的部署范围。
2. **正常 Linux 客户端路线并行作为备用设计。** 官方来源客户端＋虚拟显示＋普通页面导航＋单域名短窗接收；首先证明不做客户端补丁也能打开真实寿司郎。本人首次扫码仍可能需要；后续设备验证须正常处理。没有明确支持证据时，不购买新主机或承诺4GB足够。
3. **本机正常客户端修复是近期执行入口。** 捕获与解密名单必须保存后重新打开核对；接收器就绪和独立关闭期限必须先于页面操作。优先一份新的正常完整上下文，随后1–3家已核门店各一条独立只读查询。没有目标请求时分别报告“是否发请求／是否命中过滤器／是否收到完整头／是否产生新授权”，不能统一叫接口失效。
4. **一次真实到期恢复决定是否进云。** 更新发生在无人点击条件下；记录旧声明、更新开始／完成、实际独立接受、原采集进程身份和缺口。必须跨实际到期，不能改系统时间、伪造声明或仅修改文件版本。完整上下文的六项头要有一致来源，不拼接旧头与其他应用授权。
5. **接线后观察一整个营业日，再逐级扩店。** 服务器只收允许的公共门店观测及健康信息；永久供应端、公共投递、只读展示分别验收。全国目录完整性、调用配额与源刷新间隔仍未知，不能因三家成功立即恢复147家高频任务。

每个相同条件实验只执行一次；只有设置、来源、版本或触发动作出现可说明的新证据才重试。401进入凭证更新流程；403保留来源保护并停止；网络／界面失败保留缺口。不删除原云 halt、延长旧预算或补发积压。已通过且未改动的软件检查不重复执行，纯研究不发布新版本。

## 本机实验应如何解读

本轮三次短窗没有取得新上下文：第一次 `window_native_intake_failed`，第三次明确 `surge_window_timeout`；旧暂存revision1不变，均有关闭确认。随后重新打开捕获关键词发现仍是原CRM规则，此前 AX `setValue` 的可见文本变化没有落入保存结果。改用正常键盘编辑、Tab提交及内外两层确认后，重新打开已看到 SAPI 规则；这证明保存方式已修正，不证明前三次没有发SAPI请求，也没有证明无人续期。

具体实验和私有证据位置记在交接历史。不要向其他AI发送原始HAR、完整请求头、账号资料或CA私钥；可转发 [AI_TECHNICAL_BRIEF.md](AI_TECHNICAL_BRIEF.md)。

## 当前阶段目标

2026-10-11后续：本人回复“已修改并恢复”，工具读出新阶段objective、status=active。以下blocked段落是此前时点，当前不再等待面板操作。指定店接入及实际失败见交接E0407；外部回复见[DeepSeek复核](DEEPSEEK_SUPPLY_REVIEW.md)，下一具体范围见[YYB本机隔离准备](YYB_LOCAL_TRIAL.md)。

先打通官方查询凭证无人更新与1–3家门店持续采集：取得同一本人账号／目标应用的完整新上下文，经过独立查询并让原进程跨真实到期恢复；然后把公共观测可靠送到现有服务器，在原统计地址显示。失败按阶段给出具体原因、有限下一实验和需要人工的动作，不重复同条件抓包、未改变的全套检查或等待数据时停下开发。保留历史数据、来源隔离、TLS校验与正常认证边界；全天／全国和预测模型依次推进，正式版本只按实际证据确认。

此目标文字已作为项目阶段计划保存，**不表示目标面板已经更改**。2026-10-11读取已有目标仍为blocked，当前工具只有创建／读取及complete、blocked、paused状态更新，没有已有objective编辑或active恢复参数；不为替换目标把原目标虚假标记完成。按实际官方[目标命令说明](https://learn.chatgpt.com/docs/developer-commands)可由本人用目标编辑入口／`/goal edit`修改，再恢复目标。研究工作无需等待这个面板动作。
