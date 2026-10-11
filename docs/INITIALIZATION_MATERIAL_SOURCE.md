# 初始化材料与首次查询上下文

2026-10-11 E0460／rc82：这是已实现的原创程序接口及合成检查说明。真实材料仍在本机原审计进程内存，没有导出或上传；服务器首次官方查询、跨期限和重启恢复尚未完成。[服务器迁移计划](CLOUD_OFFICIAL_SUPPLY_PLAN.md)中的部署和授权条件继续适用。

## 作用与输入

`materialsource.prepare_material_source` 从调用者明确指定的私有文件准备请求材料，不扫描微信目录、不执行官方代码、不抓包、不取账号或旧查询凭证。调用者分别固定应用、来源版本和来源包SHA256；配置声明逐项匹配，声明本身不认证材料已审计或已获授权。

私有文件最大16KiB、POSIX目录0700及文件0600，沿既有路径／身份／前后读取保护。重复JSON字段、非有限数、额外字段、非ASCII头、未知表达式均拒绝；每次使用重新核对文件，内容或权限改变即停。不存在默认官方值，公共仓库只含合成例子。

严格schema1字段为 `schema_version/target_app_id/source_version/source_package_sha256/timezone/signature_key/long_token/app_client/device_id/app_version/user_agent/referer/initialize_content_type/query_content_type/date_header`。不接受 `authorization`、旧JWT、任意目的地址或可执行脚本。真实材料、材料指纹与身份字段不进入公共日志、统计接口或大模型。

`date_header` 只接受 `algorithm/key_hex/iv_hex/base/pieces`；算法固定AES-CBC／PKCS7／Base64，pieces只能是有限的ASCII字面量或 `year/month/day/derived` 字段。日期明确Asia/Shanghai，`derived = base - year + month - day`；不依赖服务器默认时区或旧日期头。协议参数由授权材料给定，不能把固定IV解释成文件存储加密方案。

请求使用新code与当前秒、新的六位随机nonce，以及正常标量按键排序的HMAC-SHA256签名。初始化五头和七字段全部由材料生成；初始化JSON与后续GET的form内容类型分开指定。不输出材料或请求正文，也没有包导出／上传工具。

## 首次发布与后续更新

`sessionverification.prepare_bootstrap_verifier` 在获取code前检查明确应用／客户端、私有空目的文件及父目录身份。只在一个指定门店GET成功且正常解析后写完整revision1；查询401／403、错店、声明不足30秒、竞态替换均停止。意外已有文件不读正文或覆盖，不制造占位token。

`bootstrap_from_yyb` 将显式目标供应、初始化、独立GET和首次原子保存连起来；schema2供应配置仍严格为 `schema_version/protocol_token/ref/app_id`，账户引用需调用者明确提供，不自动枚举。提交成功但持久化不确定单独报告，保留已写文件，不立即重取码或回退。

之后继续用已有基线的 `renew_from_yyb`；该入口供应配置保持schema1。新增可选 `query_content_type` 明确区分JSON初始化与form查询，省略时保留旧回调行为。同一个 `CredentialSource` 可读取递增revision，已有任务预算、截止、错配与401／403保护不改变。

私有来源可重新准备，但并不自带调度、任务游标、自动授权、冷启动制品安装或服务器持久会话。程序上的可重建来源不是服务器实际重启证据。

## 密码库与检查范围

基础运行依赖仍为空；此来源需要可选 `protocol` 依赖 `cryptography==50.0.2`。部署前另核对目标平台的依赖制品和安装，不能因本机已安装而称服务器具备后端。实现依据 [cryptography对称加密接口](https://cryptography.io/en/latest/hazmat/primitives/symmetric-encryption/)与[PKCS7接口](https://cryptography.io/en/latest/hazmat/primitives/padding/)；AES首块使用 [NIST SP800-38A F.2.1](https://nvlpubs.nist.gov/nistpubs/Legacy/SP/nistspecialpublication800-38a.pdf) 的公开测试向量检查。

新增合成检查覆盖首次保存、意外已有文件、父目录替换、401／403、持久化不确定、日期边界、材料改变和重新准备来源后原读者接受更新。日期变化链路模拟中国午夜前后，不代表真实过期服务行为。默认无密码库的环境会跳过两个真实AES检查；具备50.0.2的本机环境另实际执行这两项。已安装包的首次发布链路使用合成密码后端，实际网络0；真实材料等价性与云运行另验。

## 下一步

准备新的具体材料迁移授权范围和正常云账户接入，不沿用181包“不输出／上传”授权做导出；原181包已耗用额度不重读。经过明确授权才可从现有正常材料制备私有文件，限定接收服务器、传输与清理方式。先证明首次云查询，再交接低频三店计划，最后以电脑离线后新记录、云读者跨期限与重启恢复完成本阶段。
