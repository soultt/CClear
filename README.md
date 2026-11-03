# CClear · C 盘空间管家

一个面向 Windows 的磁盘空间治理工具：**清垃圾、搬大文件、迁目录、迁应用**，迁移的同时自动
**同步注册表、快捷方式、系统服务路径**，并在原位置留下 NTFS 目录联接，让程序照常运行。

界面在**独立程序窗口**中呈现（内嵌 Microsoft Edge WebView2 渲染引擎，不安装、不调用任何外部浏览器）；
后端是 FastAPI，只监听 `127.0.0.1`，不对外暴露。

> **仓库地址**：<https://github.com/soultt/CClear> ·
> 下载预编译版本 <https://github.com/soultt/CClear/releases/latest> ·
> 问题反馈 <https://github.com/soultt/CClear/issues> ·
> 更新说明 <https://github.com/soultt/CClear/releases> ·
> 应用内的「关于」页面也提供同样的入口，点击后一律用系统默认浏览器打开。

---

## ✨ 功能

### 垃圾清理

17+ 类清理规则：**15 类核心清理 + 3 组额外缓存**：

| 组别 | 清理项 | 风险等级 |
|------|--------|----------|
| 临时文件 | 用户临时文件、系统临时文件、崩溃转储、缩略图缓存 | safe |
| 隐私清理 | 回收站、最近使用记录、浏览器上网缓存 (系统) | safe |
| 浏览器 | Chrome、Edge、Firefox、其他浏览器缓存 | safe |
| 应用缓存 | 微信缓存、QQ 缓存、AppData 冗余文件 | caution |
| 开发缓存 | 开发工具临时目录、包管理器缓存、构建缓存 | caution/advanced |

扫描后支持在弹窗内**查看文件列表、进入文件夹浏览**，点击文件夹可逐级导航。

### 注册表清理（独立页面）

9 类注册表残留检查：失效的用户/系统启动项、App Paths、SharedDLLs、字体注册、最近打开记录、用户/系统卸载信息、失效的 COM 组件。项目按「安全 → 注意 → 高级」分区排列，默认勾选安全项。

### 大文件转移

扫描 C 盘（可选包含 `C:\Windows`）中超过指定体积的文件，批量迁移到其他磁盘，校验后删除源文件，并在原位置尝试创建符号链接；也可直接删除到回收站。

### 目录迁移

任意目录（如 `Downloads`、`AppData\Local`、游戏目录）整体搬到其他分区：`复制 → 校验 → 删除源 → 建目录联接 → 改写注册表 → 更新快捷方式`。

### 应用迁移

从注册表枚举已安装程序（含 32/64 位视图、用户目录应用），识别安装目录与可迁移性，一键迁移并同步注册表引用（UninstallString / InstallLocation / App Paths / Run 启动项 / 服务 ImagePath 等）、开始菜单与桌面快捷方式。

### 其他

| 模块 | 说明 |
| --- | --- |
| 迁移记录 | 每一次迁移都会写入日志，包含注册表备份目录；数据未被修改前可**一键还原**（含注册表回滚）。 |
| 设置 | 默认目标盘、迁移分组名、大文件阈值、是否创建联接 / 同步注册表 / 指纹校验 / 迁移前结束进程。 |
| 关于 | 版本、启动模式、管理员状态、操作系统与数据 / 备份 / 日志路径一览，并提供 GitHub 仓库入口。 |

---

## 🔒 安全性设计

* **迁移前自动备份注册表**：受影响的键导出到 `%LOCALAPPDATA%\CClear\backups\<时间戳>\`。
* **目录联接兜底**：即使某些程序把路径写死在文件里，联接也能保证旧路径可用。
* **校验后再删源**：比对文件数量与总大小（可选逐文件指纹），不通过就中止并保留源数据。
* **受保护路径黑名单**：`C:\`、`C:\Windows`、`WinSxS`、`System32`、`Program Files` 根目录、`WindowsApps` 等禁止迁移。
* **磁盘空间预检**：目标剩余空间不足时直接拒绝执行。
* **只监听回环地址**：Web 服务不对外网开放。

---

## 🚀 运行

### 直接下载（推荐）

不需要 Python：从 [Releases](https://github.com/soultt/CClear/releases/latest) 下载 `CClear-Setup-*.exe`（安装包），双击安装后运行。

### 环境要求

* Windows 10 / 11（64 位）
* **Microsoft Edge WebView2 运行时**：Win11 及打过补丁的 Win10 通常已预装。缺失时程序会自动退回到系统默认浏览器。

### 启动方式

| 方式 | 说明 |
| --- | --- |
| 双击 `CClear-Setup-*.exe` 安装后运行 | 安装包方式，推荐 |
| 双击 `CClear.exe` | 单文件免安装版，双击即用 |
| `python main.py` | 开发模式启动，后台服务运行，界面由 Electron 壳或 WebView2 打开 |
| `python main.py --browser` | 使用系统默认浏览器打开界面 |
| `python main.py --no-window` | 只启动后台服务，不打开界面 |
| `python main.py --elevate` | 请求管理员权限后启动 |

### 打包

```bat
build-exe.bat               :: 打包后端 CClear-backend.exe（单文件）
build-electron.bat          :: 打包 Electron 安装包 CClear-Setup-*.exe
```

启动后界面在独立窗口中打开（窗口标题 `CClear · C 盘空间管家`）；关闭窗口即退出程序。**不依赖浏览器**，也不会弹出浏览器标签页。

窗口使用自绘标题栏（原生标题栏已移除）：按住标题栏拖动可移动窗口，双击标题栏最大化 / 还原，右侧按钮分别是最小化、最大化 / 还原、关闭。窗口四边与四角也能拖动缩放。

> **什么时候需要管理员权限？**
> 清理系统缓存（`C:\Windows\Temp`、Windows 更新缓存、Prefetch、DISM）、创建符号链接、改写 `HKLM` 下的注册表项。应用会自动检测并提示，普通权限下会跳过受限步骤。

---

## 🧪 测试

```bat
python tests\smoke_test.py          :: 核心引擎（真实执行一次迁移+还原）
python main.py --no-window --port 8791
python tests\api_test.py            :: HTTP API 全流程
```

---

## 📁 目录结构

```
CClear/
├── main.py                 启动入口（独立窗口 / 端口探测 / 权限提升 / 日志）
├── CClear.spec             PyInstaller 打包配置（单文件、无控制台、UAC 管理员）
├── CClear.ico              应用图标
├── requirements.txt        Python 依赖（fastapi、uvicorn、psutil）
├── LICENSE
├── packaging/hooks/        PyInstaller 钩子
├── app/
│   ├── server.py           FastAPI 路由与作业调度
│   ├── core/
│   │   ├── cleaner.py      清理规则库与执行器
│   │   ├── scanner.py      大文件扫描、目录热点分析
│   │   ├── mover.py        迁移引擎（复制→校验→联接→回滚）
│   │   ├── registry.py     注册表检索 / 备份 / 改写
│   │   ├── regclean.py     注册表残留扫描 / 分级 / 备份清理
│   │   ├── appmover.py     应用枚举、分析、应用迁移
│   │   ├── jobs.py         后台任务与进度
│   │   ├── journal.py      迁移日志与统计数据
│   │   ├── util.py         路径/长路径/目录体积/指纹
│   │   └── winutil.py      权限、磁盘、联接、robocopy 复制、快捷方式
│   └── web/                前端图标
├── tests/                  冒烟测试与 API 测试
├── build-exe.bat           打包后端脚本
└── build-electron.bat      打包 Electron 安装包脚本
```

---

## ⚠️ 使用须知

1. **迁移前关闭相关程序**，被占用的文件无法移动。
2. 目录联接是 NTFS 特性，**目标分区必须是 NTFS**。
3. 如果目标盘是移动硬盘/U 盘，请勿拔出，否则原路径会失效。
4. 迁移只是换分区，**不会重复释放空间**：只有跨分区迁移才会释放 C 盘空间。
5. 建议先在少量数据或虚拟机上验证流程。
6. `Windows.old`、组件存储 (DISM) 清理属于不可逆操作，请确认后再执行。
7. 注册表清理前会逐项导出备份（`%LOCALAPPDATA%\CClear\backups\regclean-*`），但仍建议先创建系统还原点；「高级」类别涉及 COM 组件注册，请谨慎选择。

---

## ❓ 常见问题

* **组件存储清理 (DISM) 扫描出数字却"清理不动"？**
  DISM 的 `/Online` 操作（分析、清理）**必须管理员权限**，普通权限下会直接返回错误码 `740`。请点击界面右上角「获取管理员权限」，在 UAC 提示中确认后程序会以管理员身份重新启动。扫描出的数字是 `WinSxS` 目录大小（含大量硬链接），**并非可回收量**；以管理员身份启动后，本应用会改用 `DISM /AnalyzeComponentStore` 显示真实的"可回收组件"大小。

* **点击"浏览"没有反应/没有出现选择窗口？**
  "浏览"使用界面内自带的目录选择器（可选盘符、逐级进入、双击打开文件夹、显示隐藏目录），不依赖 Windows 原生对话框。如果点击仍无反应，请关闭窗口后重新运行。

---

## 💖 赞助

如果这个项目对你有帮助，欢迎请作者喝杯咖啡 ☕

[在爱发电赞助](https://afdian.com/a/yhbsoul)

---

## 📜 License

MIT © 2026 soul yu — 详见 [LICENSE](LICENSE)。

项目地址：<https://github.com/soultt/CClear>（欢迎 fork、提交 issue 与 PR）.