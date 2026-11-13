# Fieldwork 本地桌面版

Electron 只是桌面窗口；应用包包含 Python 3.12、FastAPI 后端、前端构建及模型配方。双击应用后启动本机后端，连接 `http://127.0.0.1:18767`。不加载线上工作台，也不需要先从源码运行项目；网页版和服务器版分别保留。

本地文件、草稿、作品和实例配置保存在 macOS 的 `~/Library/Application Support/Fieldwork/data/`。日志位于应用的日志目录，可从「帮助 → 后端日志」打开。桌面窗口禁用 Node integration、启用沙箱及上下文隔离，不向页面暴露系统调用接口；非本地链接交给默认浏览器。

桌面版继续使用本机已有的 `colab1`／`colab2`／… 启动器、各账号凭证、SSH 私钥和云盘只读配置。它们不放入应用包或源码，安装到另一台电脑时应另外配置。应用内置 Python 只运行工作台，Colab CLI 仍由各启动器指定自己的 Python。

Colab GPU 和模型权重源仍需联网；“本地版”指编辑界面、控制后端及数据在本机，不表示模型在 Mac 本地计算。桌面版没有绑定线上域名或线上登录账号，模型 API 客户端使用本地 `18767` 端口和该本地数据目录中的服务密钥。

## 使用

- 直接打开 `Fieldwork.app`；按 ⌘R 刷新工作台，标准编辑、缩放和窗口菜单保留。
- 关闭窗口后应用在 macOS 后台保留，再点 Dock 图标可重新打开；⌘Q 才退出应用及其本地后端。
- 退出遵循工作台原有的“退出时释放机器”设置，等待正在进行的分配／释放任务收尾。服务器端实例和外部会话不会被桌面版接管。
- 网页界面随应用包更新，本地版不会随着服务器更新而自动改变。

## 从源码打包

当前打包脚本面向 Apple Silicon macOS，需要已有 `uv`、Node.js 和 Python 3.12 托管运行时：

```sh
npm --prefix frontend ci
npm --prefix desktop ci
npm --prefix desktop run package:mac
```

产物位于 `desktop/dist/app/Fieldwork-darwin-arm64/Fieldwork.app`。打包只复制后端源码、前端产物、模型配置及 Python 运行依赖，不复制 `.data/`、私钥、账号、运行日志或云盘私有索引。当前使用本机 ad-hoc 签名，未进行 Apple Developer ID 公证。

初次迁移旧的本地项目数据时，先完成任务并释放原项目所属机器，退出所有本地后端，再执行：

```sh
uv run python desktop/import-local.py /path/to/old/.data "$HOME/Library/Application Support/Fieldwork/data"
```

导入使用 SQLite backup 保留配置、作品及端点身份，拒绝覆盖已有桌面数据；不导入登录会话和日志，不复制活动机器的归属，也不复制账号凭证。之后直接用桌面版管理自己的新实例即可。
