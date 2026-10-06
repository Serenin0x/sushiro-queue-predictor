# 本机投递与签名回执保存

`packet-deliver-local`明确投递一个已保存的私有公共字段包，仅连接`127.0.0.1`的指定端口；使用独立接收口令，不读取寿司郎查询凭证。配合[有界接收服务](PACKET_RECEIVER.md)验证“归档、对应回执、发送端保存确认”，尚未部署远程服务。

```sh
sushiwait packet-deliver-local --input "$PRIVATE_PACKETS/page.json" \
  --receiver-token-file "$PRIVATE_INGEST/receiver.token" --port 12345 \
  --confirmation "$PRIVATE_CONFIRMATIONS/page-confirmation.json"

sushiwait receipt-check --input "$PRIVATE_PACKETS/page.json" \
  --receiver-token-file "$PRIVATE_INGEST/receiver.token" \
  --confirmation "$PRIVATE_CONFIRMATIONS/page-confirmation.json"
```

端口必须是明确的1–65535，不能填写域名或远程URL。确认目录须已存在且本人0700；包、独立口令与确认均为本人0600/0400单链接文件。新确认输出不覆盖已有文件、不与输入或口令同路径。命令不生成或输出接收口令，不把查询配置当口令；无重定向、Cookie或外部查询。

## 回执对应关系

rc21的HTTP成功回执为schema2，使用独立接收口令对规范JSON作HMAC-SHA256（不包含HMAC字段本身）；不是寿司郎的签名或身份认证。调用方核对版本、全部允许字段、整包SHA、每条观测ID/内容SHA与顺序，以及新增和重复数量之和。旧schema1未签名HTTP回执不再作为可保存的成功确认；接收模块纯函数仍提供schema1内部归档结果。

验证成功后在新0600文件中原子保存确认并核对持久性。`receipt-check`离线重开原包、独立口令和确认文件，复核签名与对应范围；它不联网、不查询当前归档库。HMAC只说明相同独立密钥的持有者生成了回执，不能防御已拿到该密钥的本机进程，也不认证上游数据或收到确认后的库保留情况。确认外层保存时间和端口只是本机元数据，不是接收端签署的时间证明。

## 失败与保留

发送端网络窗口由独立定时器在5秒截止，并关闭保存的连接socket；连接及单次读写另有超时。慢响应逐字节到达也不能无限延长窗口。磁盘保存、系统调度与清理仍可能额外耗时，不承诺全流程硬实时。

连接前失败标记`receiver_commit_status=not_attempted`；POST发出后失败通常为`unknown`，因为接收端可能已经提交。完整回执验证后为`reported_committed`，即使之后本机确认保存失败也保留这个状态。保存后持久性确认失败需区分已提交与持久性未知，不伪装成没有保存。

所有情况下保留原包，不自动重试、删除、推进上传游标或更改[待确认目录](PENDING_PACKETS.md)。需要重试时以另一个新的确认路径原样投递同包，接收端按观测ID去重；不要改变原包内容以规避冲突。一次签名确认不代表永久备份、远程部署、来源新鲜度、真实训练标签或可用预测。

## 检查范围

检查涵盖合成本机HTTP发送/签名/保存/离线重开、已提交回执丢失后的重复确认、慢响应截止、拒绝重定向/Cookie/重复头和坏JSON、签名与逐观测对应不符、私有路径及保存前后故障。独立安装检查使用合成签名及替代传输函数，明确不建立socket；真实安装包本机投递另验，证据见[项目交接](PROJECT_HANDOVER.md)。
