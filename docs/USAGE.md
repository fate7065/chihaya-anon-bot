# 使用说明

## 安装与启动

Python 需要 3.11 或更高版本，Linux 需要包含 venv 模块。安装器只创建项目自己的 `.venv`，已有配置与密钥会保留。

Windows：双击 `install.cmd`，再双击 `run.cmd`。Linux：执行 `sh install.sh`，再执行 `sh run.sh dashboard`。

活动面板地址是 `http://127.0.0.1:8788`，管理口令位于 `local/secrets/dashboard.key`。程序重启后重新登录。

## 聊天 API

把 API 密钥保存为 `local/secrets/chat.key`，文件只放密钥本身。编辑 `local/config/worker.json` 中的 `settings.base_url` 和 `settings.model`。默认使用 DeepSeek，也可以改为兼容 Chat Completions 的 HTTPS 接口。

设置好此文件后，活动面板会开放逐条生成内容评价的入口。点击生成会把选中的公开片段发送到所配置的聊天服务。

向量和视觉服务分别在 `local/config/release.json` 的 `embedding`、`vision` 配置；各使用自己的 `base_url`、`model`、`key_file` 和 `allow_transfer`。其默认值为 null，添加服务后再设置输入外传开关。

## QQ 接入

1. 独立安装并登录 NapCatQQ，开启本机 OneBot HTTP 接口。
2. 在 `worker.json` 填写机器人 QQ 号、网关端口及私聊范围。
3. NapCat 的 HTTP Access Token 对应 `local/secrets/gateway.key`；上报密钥对应 `local/secrets/webhook.key`。
4. 将事件上报地址设为 `http://127.0.0.1:8765/events`，按 OneBot 11 配置 Bearer 或签名认证。
5. Windows 双击 `run-qq.cmd`；Linux 执行 `sh run.sh qq`。

`private_mode` 为 friends 时按当前好友列表核对；设为 allowlist 时，只处理 `allowed_users` 中的 QQ 号。服务会先核对登录账号，消息发送状态持久化保存。

## 语音

文字聊天保持中文，语音提供中文与日语两种选项。中文配方使用 `s1v3.ckpt` 与千早爱音 V2 SoVITS，日语使用千早爱音 GPT-SoVITS V2 的 GPT/SoVITS 配对。整句策略使用 cut0。

准备两个本机语音服务：中文默认 `127.0.0.1:9892`，日语默认 `127.0.0.1:9893`。参考音频与逐字日文台词要配对；默认模板的参考路径是项目内 `models/reference.wav`。配置 `local/config/voice-zh.json`、`voice-ja.json` 中的参考音、台词、输出目录及端口。所用服务需要兼容 GPT-SoVITS 的 `/tts` WAV 接口。

语音服务准备好后，Windows 使用 `run-qq-voice.cmd`；Linux 使用：

```sh
sh run.sh qq --voice-options local/config/voice-options.json
```

私聊指令：

| 指令 | 功能 |
|---|---|
| `/中文语音` 或 `/语音 中文` | 切换为中文语音 |
| `/日语语音` 或 `/语音 日语` | 切换为日语语音 |
| `/语音语言` | 查看当前语音语言 |
| `/文字` | 使用中文文字回复 |
| `/语音` | 恢复使用已选语言的语音回复 |
| `/回复模式` | 查看当前回复方式 |

默认语音为日语。每条消息的语言在开始处理时固定，发送中的分段不会因下一条切换指令而改变。

## 上网内容与评价

启动读取任务的进程时，将 `ANONBOT_ACTIVITY_DB` 指向 `local/state/activity.sqlite3`；面板和读取任务使用相同数据位置。B 站匿名读取任务会保存公开标题、简介或字幕片段，分别显示实际来源和模拟来源。

评价使用现有角色卡，只基于当前保存的片段；引用必须能够在原文中找到。每条内容只生成一次，每小时最多 20 条；刷新和重复点击不会重复请求。评价保存在本地，不自动向平台发布。

## 服务器

推荐先用 Linux 原生 Python 方式运行。QQ 接收端和语音服务保持在回环地址，通过 SSH 隧道或 HTTPS 反向代理访问面板。公开访问面板时，将 `release.json` 的 `origin` 改成自己的 HTTPS 域名，反向代理保留 Host。

`deploy/` 提供 Linux Docker Compose 模板；配置目录、状态目录和 secret 分别挂载。容器使用普通用户，根文件系统只读。NapCat 与语音模型服务独立部署，QQ profile 由操作者显式启用。

## 备份与更新

配置和运行数据位于 `local/`，更新源码时保留该目录。升级前保存上一版源码与本地数据快照；恢复时先在独立目录核对，再切换服务。

活动元数据可使用：

```sh
.venv/bin/python -m anonbot.release backup-activity --source local/state/activity.sqlite3 --destination backups/activity-new
.venv/bin/python -m anonbot.release restore-activity --source backups/activity-new --destination restored/activity.sqlite3
```

Windows 将 `.venv/bin/python` 换为 `.venv\Scripts\python.exe`。

聊天库、内容与评价库、回复偏好和密钥文件需要分别保存。对这些文件做整体备份时先停止相应服务。保护好备份与密钥，不放进公开仓库。
