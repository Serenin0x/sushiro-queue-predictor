# 调试关闭后提交私有暂存

rc9新增 `context-promote`。完整的新查询上下文先存到本机独立私有暂存；临时调试结束并确认关闭后，才提交给采集程序。这把已实测正常8的协调步骤整理成可安装组件，避免接收器过早改主配置、采集随即恢复而遇到代理证书。它不取得新凭证、不操作微信，不替代完整更新控制器。

```sh
sushiwait context-promote \
  --credentials-file /absolute/private/current.json \
  --staged-file /absolute/private/staged.json \
  --expected-revision 8
```

路径必须显式指定，直接父目录0700/0500、文件0600/0400、本人所有且单硬链接，不经过符号链接；文件沿用严格16KiB/schema1/gateway完整格式。expected-revision应为当前实际主版本，示例不是自动使用的默认值。暂存版本须更高，授权须不同且声明剩余超过30秒；声明未验签，不是官方授权真实性认证。

工具先读取既有Surge的三个白名单开关，任一ON或未知则拒绝；验证两份完整上下文、当前版本、同应用referer及app-client后，再次核对OFF。app-code和正常页面版本允许更新，不拼接旧字段或环境配置。基于已验证的整组对象序列化，避免重新打开暂存文件复制时被替换。复用原私有原子写入器，新增可选expected-current检查，在目录独占协作锁内重新比较完整旧上下文、文件身份、版本及到期保护；不符合时保留主文件。

原子替换完成后核对新上下文和三个OFF。成功输出revision、committed、durability_confirmed、debug_off_confirmed、staging_preserved及服务端接受仍unverified，不输出路径、原始头、应用ID或凭证。目录持久化确认失败保留committed=true/durability=false、退出1；提交后回读/OFF核对失败同样明确committed=true，不自动重试、回退旧令牌或删除暂存。采集器是否接受及独立GET是否成功须另行观测。

目前只适用于已观察Surge Mac环境。它不改变任何开关、名单、证书或路线，不打开客户端、不发上游查询。OFF状态核对与目录锁仅协调遵守协议的进程；其他操作者可能在检查后再次开启，不能宣称消除所有竞态。开启前仍须等待采集实际暂停并运行[独立关闭保护](SURGE_GUARD.md)，结束仍须恢复原名单和关闭小程序。

26项专项0.051秒、完整421项4.683秒通过，无跳过/JS已含。覆盖真实私有文件及原子更改、协作写者竞争、暂存更换、提交前真实声明到期、同路径/权限/符号链接/硬链接/超限/缺字段、OFF变化、提交后核对失败、持久化不确定及无凭证泄漏；均为合成资料、没有Surge或生产查询。第一次24项有同路径错误分类失败，修正后补两项成为26，历史保留。最终19模块包及checkout外隔离安装、帮助/非法版本参数native0/socket0已验。本机安装后的新命令对本人已正常取得的5/8独立私有副本实测：三次读取真实Surge开关OFF、完整原子提交/durable成功、暂存保留、运行中主8未改动、上游请求0。它验证本机读状态与副本提交，不能说生成新凭证或通过新命令恢复主采集，也不能把正常8私有辅助脚本当此新命令实测；见E0107。

正常接收见[SURGE_INTAKE](SURGE_INTAKE.md)，原采集恢复见[COLLECTION_TASKS](COLLECTION_TASKS.md)，后续路线见[LONG_TERM_COLLECTION](LONG_TERM_COLLECTION.md)，全部版本/编辑与实测见[PROJECT_HANDOVER](PROJECT_HANDOVER.md)。
