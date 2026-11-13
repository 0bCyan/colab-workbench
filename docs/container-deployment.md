# 容器部署与 SSH 访问

此部署运行工作台控制端；模型计算仍由按需申请的 Colab GPU 完成，服务器无需安装 CUDA。当前仅通过 SSH 转发访问，不提供公网入口。OAuth 登录、域名及 HTTPS 反向代理留待后续实现。

## 组成

- 多阶段镜像：Node 构建前端，Python 3.12 运行 FastAPI。
- 工作台依赖使用 `uv.lock`；独立 `/opt/colab-cli` 固定 Google Colab CLI 0.8.0，依赖清单含哈希。
- 内置 OpenSSH 客户端和 rclone；以 UID/GID 10001 运行，只读根文件系统。
- `runtime/` 是唯一持久化挂载，包含数据库、作品、云盘索引、账号凭证及专用 SSH 身份。不得提交或放入构建上下文。
- 宿主机仅绑定 `127.0.0.1:8765`，容器内监听 `0.0.0.0:8765`。不改 Caddy、域名、防火墙和其他应用。

## 准备账号和云盘

先在部署目录创建数据目录，确保属主为 10001:10001，目录权限 0700、凭证 0600：

```sh
mkdir -p runtime/.local/share/colab-accounts/credentials runtime/data/cloud
chown -R 10001:10001 runtime
chmod 700 runtime
```

通过私下 SSH/SFTP 将已登录账号的 ADC JSON 放到：

```text
runtime/.local/share/colab-accounts/credentials/1.json
```

首次先迁移账号 1。容器启动时按数字凭证文件建立 `colab1` 等入口；后续添加 `2.json`、`3.json` 后重建或重启容器即可发现新账号。各账号单独设置 ADC、Scopes、会话文件、历史与设置，禁止通过启动器切换到其他认证方式或执行原地更新。

不复制本机正在运行的 Colab 会话配置，也不把 macOS 的 Python 二进制或绝对路径搬进 Linux。容器重新安装同版 CLI，使用相同的多账号隔离机制。凭证能否直接使用应以服务器上的实际账号查询结果为准；失效凭证仍可能需要重新登录。

Drive 路线另外需要本机已有的只读配置和私有索引：

```text
runtime/.local/share/llm-workbench/drive-reader.conf
runtime/data/cloud/<preset>.json
```

只传 `drive.readonly` 授权，不传上传／写入授权。首次启动自动生成一对服务器专用 Colab SSH 密钥，保存在 `runtime/.ssh/`；不需要复制个人电脑 SSH 私钥。账号登录凭证不会写入镜像层。

## 启动与只读验证

```sh
docker compose build
docker compose up -d
docker compose ps
docker compose exec workbench colab1 version
docker compose exec workbench colab1 usage
docker compose exec workbench colab1 sessions
```

`usage`、`sessions` 验证已有凭证及服务连通性，不申请 GPU。只需确认返回正常，不应公开原始账号余额和会话输出。页面访问本身也不会自动申请 Colab。

从个人电脑建立转发：

```sh
ssh -N -L 127.0.0.1:18765:127.0.0.1:8765 <ssh-user>@<server-host>
```

打开 `http://127.0.0.1:18765`。本地已有 8765 服务时使用 18765 可避免冲突。LLM 客户端也经该转发端口使用页面展示的实例路径和服务密钥。

## 更新、停止和备份

```sh
docker compose build
docker compose up -d
docker compose logs --tail 80 workbench
```

容器更新会正常关闭后端。若 `stop_on_exit` 开启，会尝试释放本工作台所属的云端机器；更新前应完成任务并确认退出策略，不能把更新当作单纯静态页面刷新。Compose 给后端 8 分钟退出窗口，以处理分配中或释放中的实例。

数据库和作品存于 `runtime/data`，账号和 Drive 配置在同一私有持久卷中。备份必须私下保存，使用 SQLite backup API 或停止容器后备份，不能只拷贝活跃 SQLite 主文件而遗漏 WAL。恢复时不要覆盖其他控制端的新令牌或把旧运行状态当作现存 VM。

当前没有公网用户认证。后续绑定域名前，需要先实现 OAuth、授权用户限制、回调校验及相关代理头／Host 策略，再开放入口。

## 本次部署验证范围

2026-11-13 已在 Linux amd64 服务器部署。colab1 的 CLI 0.8.0 版本、账号查询及会话查询成功，迁移 ADC 后无需重新登录；独立只读 Drive 授权在服务器成功刷新，文件元数据及 1 KiB Range 读取通过。服务器页面经 SSH 隧道验证了图片精度、比例与尺寸切换。此次未申请 GPU，因此尚未验证服务器创建新 VM、连接 Colab SSH 及完整模型推理。

macOS 若开启 Clash TUN，可在 SSH 命令增加 `-o BindInterface=en0`，使用实际物理网卡绕过 TUN；网卡名称按本机情况替换，不需要关闭整个代理。
