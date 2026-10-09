# 原生Linux单店隔离试验

2026-10-09 11:04最新状态：rc57核心提交3a99a2bd及精确CI37874603963四job通过；三版各1533项，新增网关／归档工具的检查另记，不借核心CI。本人已明确批准指定单端口公开非个人只读统计，网关已实际可访问、每30秒更新，管理／票据／文件与写方法仍拒绝；云端共有十二独立采集、hub、watch、只读网关和有限每日归档16个服务。11:00自动开采，11:03:58核对十二店全部running／alive、每店4成功0失败8GET，合计48组96GET，前端已同步；完整全天和连续72小时尚未验收。每店60秒／1500组3000GET、48小时原截止10-11 10:16:19不变，今天11–22／明天自然周六10:30–22按user_assumed默认表运行。11:01／11:04／11:08三次只读开门自检由服务器执行；10-09／10-10各22:05自动保存每日副本及摘要，原始库持续保留，不依赖本人电脑或AI在线。有限试验不是永久每日控制，异机备份／来源认证／真实结果标签及ETA仍未完成。见交接E0337–E0338、原生云试验和固定统计入口说明。

当目标主机访问Docker官方注册表超时，可以使用已核验的通用Python wheel和原生systemd运行相同采集程序。不能把Docker安装成功当成镜像构建成功，也不关闭TLS或改用来源不明的镜像。

2026-10-09本人上海Ubuntu24.04.4／x86_64主机已实际安装rc57并启用十二个独立单店、hub与watch共14个systemd服务；10:16:19创建，48小时原截止不改，开店前全部存活且0GET。旧rc56四批与hub正常停止／disable、原资料保留，避免重复写者。10:23:48健康记录十二店无关注项，服务均active、NRestarts0；仅13个回环监听。此时营业真实响应及全天质量尚待开店自然核验；详见交接E0336。

## 安装与生成

在独立源码目录准备`.venv`，仅安装明确文件和固定哈希的依赖。源包／wheel的SHA256与本机冻结结果匹配后再安装。`pip --no-index --no-deps`安装本人核验wheel；server依赖使用`deploy/requirements-server.txt`的固定版本与SHA，不上传微信材料。

```sh
python3 -m venv /home/ubuntu/sushiwait-release/.venv
/home/ubuntu/sushiwait-release/.venv/bin/python -m pip install \
  --no-index --no-deps /home/ubuntu/sushiwait-0.2.0rc57-py3-none-any.whl
/home/ubuntu/sushiwait-release/.venv/bin/python -m pip install \
  --only-binary=:all: --require-hashes \
  -r /home/ubuntu/sushiwait-release/deploy/requirements-server.txt
/home/ubuntu/sushiwait-release/.venv/bin/python -I \
  /home/ubuntu/sushiwait-release/scripts/check_campaign_recovery_install.py
```

核验安装包真实版本与营业专项后，选一个**全新**状态根目录。生成器只创建配置，拒绝覆盖已有根，不装服务、不执行采集。

```sh
/home/ubuntu/sushiwait-release/.venv/bin/python -I \
  /home/ubuntu/sushiwait-release/deploy/prepare_native.py prepare \
  --release-dir /home/ubuntu/sushiwait-release \
  --root /home/ubuntu/sushiwait-trial --user ubuntu \
  --prefix sushiwait-trial --first-port 18801
```

单位使用现有用户、0700状态／0600文件、只读系统／home、专属写目录、NoNewPrivileges、256MiB／64任务上限。每店独立一个campaign，背景60秒、48小时、1500组3000GET、恢复额度3；十二店合计18000组36000GET。只在默认用户声明营业表内发起实际GET；每次保存请求开始与接收时刻、原始顺序和前三位投影，原库保存完整数组。第一位叫号参考是用户约定，未观测不自动判断过号。

先`systemd-analyze verify`，检查系统中同名单位不存在，再由管理员逐个安装所生成的明确单位并`enable --now`；不要覆盖其他服务。默认`Restart=on-failure`只处理进程异常退出，业务瞬时错误由明确有界策略处理；绝不无限重建任务或清零失败。

十二个采集服务均实测就绪后，生成器`hub`模式只读其本机状态、核对范围与实际截止，生成`hub.json`及只读汇总单位：

```sh
/home/ubuntu/sushiwait-release/.venv/bin/python -I \
  /home/ubuntu/sushiwait-release/deploy/prepare_native.py hub \
  --root /home/ubuntu/sushiwait-trial
```

hub端口是first-port减1，仅127.0.0.1；stats与日历读取不另查寿司郎。未经配置本人SSH通道或其他经验证访问控制前，服务器本机地址不能直接在自己的电脑打开，不开放公网统计端口。

## 健康观察与结束

`deploy/cloud_watch.py --root /home/ubuntu/sushiwait-trial`每60秒读一次本机状态，原子保存`progress.json`，正常营业后超过180秒没有检查点推进或工作者失活标需要关注。只在状态变化打印摘要，不查询来源、读取活库、重启任务或发送通知；与采集进程独立，到原截止停止。它能发现异常，不能代替短暂恢复、对外通知或磁盘外备份。

每天实际覆盖、错误、延迟、门店当前映射及来源更新需要自然核验，不能因HTTP200就保证绝对新鲜。到期前停止和后续有界任务应明确安排，原目录全部保留；不移动活库，不通过删库重开延长预算。数据备份、验证恢复与永久每日控制尚需完成。

单位设置依据：[Ubuntu24.04 systemd.exec](https://manpages.ubuntu.com/manpages/noble/man5/systemd.exec.5.html)、[systemd.service](https://manpages.ubuntu.com/manpages/noble/man5/systemd.service.5.html)。生成器与实际服务日志、当前运行源版本和SHA一并核对；原生服务使用现有用户，与容器UID10001的历史检查分别报告。
