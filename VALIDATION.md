# 验证记录

日期：2026 年 11 月 11 日（Asia/Shanghai）。本文件只保留技术验证结果；原始账号、会话、任务标识、余额、日志、截图与作品仅在本地保存，不进入仓库。

## 已验证的功能

- 实机链路：申请 Colab G4 → 固定版本部署 → 模型下载 → SSH 隧道 → ComfyUI 提交 → WebSocket 进度 → 成片下载 → 自动释放机器。
- H3 INT8 ConvRot 普通 20 步配置：1344 × 768、124 帧、24 fps，视频完整解码，32 kHz 双声道音频非静音。此项用于验证部署链路，不是模型质量评测。
- 后端：账号隔离、机器归属、重复启动保护、关闭期间分配、队列与原生 ComfyUI 忙碌检测、任务恢复、取消、下载、空闲关机、请求来源与令牌检查。
- 作品管理：搜索、筛选、分页、重命名、单个/批量回收、恢复、永久删除与活动任务保护。
- 性能看板：账号隔离、样本过期清除、NVIDIA 显存单位、CPU/网络首次采样；模拟数值不作为真实性能结果。
- 创作表单：打开清空、最近草稿保留、主动恢复、尺寸预设与自定义校验、空提示词拒绝提交。
- 布局：单行顶部工具栏、提示词与分辨率控件间距，桌面和窄屏无整页滚动；检查包含 1280 × 720、1280 × 650、339 × 738、339 × 640。

## 可复现的自动检查

```bash
python3 scripts/check_publish.py
uv sync --frozen
uv run ruff check backend tests scripts
uv run pytest -q
npm --prefix frontend ci
npm --prefix frontend run build
npm --prefix frontend run test:e2e
```

自动测试使用独立临时数据库、本地 HTTP/WebSocket 模拟器与 `tests/fixtures/synthetic.mp4`，无需真实账号、登录凭证、作品或付费 GPU。CI 在 Linux 使用 Playwright Chromium，本地默认使用 Chrome。

## 开源发布验证

从 Git 暂存区导出干净源码副本，未带入 `.data/`、真实参考视频、截图或备份：

- Ruff 检查通过，53 项后端测试通过。
- `npm ci`、TypeScript / Vite 构建及 4 项 Playwright 测试通过。
- 54 个暂存文件通过发布检查；额外验证了暂存令牌拦截、强制添加运行数据拦截及检查结果不回显敏感值。
- 本地 Git 提交使用 GitHub noreply 邮箱。

## 使用边界

- 默认部署只安装固定的 H3 配置；新增模型或自定义节点需要扩展部署清单。
- 导入格式为 ComfyUI API JSON，完整节点编辑器由连接后的 ComfyUI 提供。
- 原生编辑器直接提交的任务参与忙碌检测，但不自动归档到工作台。
- 自动关机依赖本地服务存活且能连接 Colab；释放云端临时盘后，下次需重新下载模型。
- 源码仓库不包含多账号启动器、登录凭证、SSH 私钥或运行状态；它们应在每台机器单独配置。
