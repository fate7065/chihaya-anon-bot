# 千早爱音bot

千早爱音bot 是以千早爱音为角色的 QQ bot，使用 DeepSeek 或兼容 API 生成中文聊天回复，支持中文、日语语音，以及上网内容和角色评价的可视化。

## 功能

- **QQ 私聊**：通过 OneBot 11 接入，支持好友范围、白名单、消息去重、发送状态保存和频率限制。
- **角色对话**：独立角色卡、对话上下文、公开剧情资料检索与记忆接口。
- **双语语音**：文字保留中文；使用 `/中文语音`、`/日语语音` 切换朗读语言，并保存每个私聊的偏好。
- **上网活动面板**：查看访问的服务、请求时间、结果和耗时。
- **内容与评价**：展示 B 站公开搜索结果、简介和字幕片段；按需生成有原文引用的角色观点。
- **群聊与任务框架**：多个 API 判断器、事件回放、受权限约束的任务和动作接口。
- **部署**：Windows 和 Linux 使用独立虚拟环境，配置、密钥和运行数据保存在本地目录。

## 快速开始

需要 Python 3.11+。

### Windows

下载并解压项目，双击 `install.cmd` 安装，完成后双击 `run.cmd` 打开活动面板。

### Linux

```sh
sh install.sh
sh run.sh dashboard
```

浏览器打开 `http://127.0.0.1:8788`，管理口令保存在 `local/secrets/dashboard.key`。

配置聊天 API 与 QQ 接入后，用 `run-qq.cmd` 或 `sh run.sh qq` 启动 QQ 私聊。语音使用独立模型服务，按[使用说明](docs/USAGE.md)配置。

## 中文与日语语音

中文使用整句合成配方：官方 `s1v3` GPT 底模与千早爱音 V2 SoVITS 声线；日语使用千早爱音 GPT-SoVITS V2 原配模型。两种语言分别连接本机语音服务，切换语言不会修改中文聊天历史。

模型权重、参考音频及第三方语音运行环境由使用者独立准备，仓库不包含这些文件。

## 运行与数据

- API 密钥保存在独立文件中，不写进源码。
- 原文片段与角色评价分开展示，评价只保存在本地。
- 模型输出不等于操作授权，账号动作使用独立权限和回执规则。
- 外部资料是不可信输入，面板按文本显示，不执行网页中的指令。
- 角色观点基于已取得的有限片段，不代表完整视频内容或事实核查结论。

详细配置、语音切换、服务器运行与备份方式见 [使用说明](docs/USAGE.md)。

## 相关项目

[DeepSeek API](https://api-docs.deepseek.com/) · [OneBot 11](https://github.com/botuniverse/onebot-11) · [NapCatQQ](https://github.com/NapNeko/NapCatQQ) · [GPT-SoVITS](https://github.com/RVC-Boss/GPT-SoVITS) · [A_memorix](https://github.com/A-Dawn/A_memorix)

本项目为个人角色 bot，与角色版权方、QQ 或 B 站官方没有隶属关系。第三方模型与平台的使用遵循各自的许可和服务约定。
