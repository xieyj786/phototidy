# PhotoTidy 项目协作指南

## 项目位置与运行环境（先读此节）

本项目刻意将**源代码/文档**与**Python 运行环境**分开存放。打开项目时，请按下表定位资源：

| 资源 | 固定位置 | 用途与规则 |
| --- | --- | --- |
| 共享源代码与文档 | `/Users/yujunxie/Library/CloudStorage/OneDrive-个人/projects/phototidy/` | OneDrive 中的主项目目录。用于从不同终端、不同设备查看和编辑 `*.py`、`README*.md`、`requirements.txt`、`*.spec` 及本文件。 |
| T7XYJ 工作副本 | `/Volumes/T7XYJ/projects/phototidy/` | T7XYJ 磁盘上用于执行、测试及处理该磁盘媒体的项目副本。与 OneDrive 目录的源代码和文档应保持同步。 |
| T7XYJ Python 环境 | `/Volumes/T7XYJ/pythonenvs/phototidy/` | 本机虚拟环境，不在项目目录也不经过 OneDrive 同步。运行 T7XYJ 副本时使用其中的 `bin/python`。 |

当前已验证的 T7XYJ 环境为 Python 3.12.10、Tk 8.6、Pillow 12.3.0、pillow-heif 1.4.0；其非图形测试已通过。其他电脑应在其本机硬盘创建各自的虚拟环境，**绝不复制、同步或提交** `.venv/` 或 `pythonenvs/phototidy/`。

在 T7XYJ 上，推荐从工作副本目录执行：

```bash
cd /Volumes/T7XYJ/projects/phototidy
/Volumes/T7XYJ/pythonenvs/phototidy/bin/python phototidy.py
```

执行其他脚本或测试时，同样以 `/Volumes/T7XYJ/pythonenvs/phototidy/bin/python` 替代 `python`。代码和文档修改完成后，应确认其已同步回 OneDrive 主项目目录；不要把环境文件、媒体文件或运行日志同步回去。

## 项目用途

PhotoTidy 是一组本地运行的 Python/Tkinter 媒体整理工具。程序通过图形界面选择源目录与目标目录，对照片或视频进行整理、查重、拷贝或重命名。它们直接读写用户的媒体文件，因此任何修改都应优先在小型副本上验证，并默认选择“拷贝”而非“移动”。

项目不是 Python 包：各工具均为仓库根目录下可直接运行的脚本，没有 `pyproject.toml`、`setup.py` 或命令行安装入口。

## 主要脚本

| 脚本 | 用途 | 是否会移动文件 |
| --- | --- | --- |
| `phototidy.py` | 按 EXIF/文件时间将图片归档到年份、月份或相机型号目录 | 可选（拷贝或移动） |
| `photodedup.py` | 图片精确和感知查重，将重复项移到 `重复图片文件/` | 是 |
| `photorename.py` | 根据 EXIF、相机信息和可用 GPS 信息批量重命名照片 | 是（重命名） |
| `videotidy.py` | 按视频容器拍摄时间或文件修改时间归档视频 | 可选（拷贝或移动） |
| `videodedup.py` | 以文件大小 + SHA-256 查找视频重复项，并移到 `重复视频文件/` | 是 |
| `videocopy.py` | 保留源目录层级地复制视频 | 否（只复制） |

所有 GUI 入口均在脚本末尾的 `main()` 或等效启动代码中。请勿在自动化检查中启动 GUI；图形窗口需要本机桌面会话。

## 运行环境

- Python：建议 Python 3.10+；T7XYJ 已验证为 Python 3.12.10。
- GUI：Tkinter（标准库）；T7XYJ 已验证 Tk 8.6。
- 第三方依赖：`Pillow>=9.0.0`、`pillow-heif>=0.7.0`，见 `requirements.txt`。
- `Pillow` 负责图片/EXIF、dHash 和直方图处理；`pillow-heif` 用于注册 HEIC/HEIF 支持。未安装 HEIF 支持时 GUI 可能仍可启动，但 HEIC/HEIF 元数据读取能力会下降。
- 视频工具只使用标准库；PhotoRename 的地理位置查询使用 Python 标准库的 HTTP 功能，网络不可用时应保留可恢复的离线行为，不能绕过 TLS 校验。

## 首次配置

若本机尚无既定环境，可在本机硬盘（不要在 OneDrive 项目目录）创建虚拟环境。T7XYJ 已有环境，直接使用 `/Volumes/T7XYJ/pythonenvs/phototidy/`，无需重复创建。

例如在其他 macOS/Linux 机器上：

```bash
python3 -m venv /path/on/local-disk/pythonenvs/phototidy
source /path/on/local-disk/pythonenvs/phototidy/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Windows PowerShell 对应命令：

```powershell
py -m venv D:\pythonenvs\phototidy
D:\pythonenvs\phototidy\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

在 macOS 上若 `import tkinter` 失败，应安装带 Tk 支持的 Python 发行版；不要试图通过 `pip install tkinter` 解决。

## 启动与验证

从项目根目录启动所需工具。下列 `.venv/bin/python` 仅是示例：在 T7XYJ 上使用本文件开头给出的绝对路径；在其他机器上换成该机器本地环境的 Python 路径。

```bash
.venv/bin/python phototidy.py
.venv/bin/python photodedup.py
.venv/bin/python photorename.py
.venv/bin/python videotidy.py
.venv/bin/python videodedup.py
.venv/bin/python videocopy.py
```

非 GUI 回归检查：

```bash
.venv/bin/python -m unittest discover -v
.venv/bin/python _test_parse.py
```

`test_photodedup.py` 依赖 Pillow；若未先安装 `requirements.txt`，测试会以 `ModuleNotFoundError: No module named 'PIL'` 失败。

## 文件操作与安全约束

- 目标目录不能位于源目录内部；保留并测试这一保护。
- 不覆盖同名文件：使用 `_1`、`_2` 等后缀生成唯一名称。
- PhotoTidy 和 VideoTidy 的移动模式会改变源目录，并可能清理空子目录；应要求用户明确选择。
- PhotoDedup/VideoDedup 不删除重复文件，但会将它们移动至库根目录下的重复文件夹；完成操作无法自动回滚。
- 停止按钮只会停止后续任务，已完成的复制、移动或重命名不会回滚。
- 日志写入目标/库根目录，使用 `*_log_YYYYMMDD_NNN.txt` 的递增文件名，绝不可覆盖已有日志。
- 配置保存在用户主目录，例如 `~/.photodedup_config.json`、`~/.videodedup_config.json`、`~/.videocopy_config.json`；不要提交这些个人配置。

## 开发约定

- 保持 Python 标准库优先；新增图片处理能力前确认是否确实需要新的依赖。
- GUI 工作应保持耗时任务在后台线程中执行，并用队列将进度/日志回传 Tk 主线程；不要从工作线程直接更新 Tk 控件。
- 处理不可信或损坏的媒体元数据时必须容错，单个文件错误不应中断整批任务。
- 所有路径都可能包含中文、空格和跨平台分隔符；文件读写使用 UTF-8，日志和配置写入时使用 `ensure_ascii=False`（如适用）。
- 修改整理、查重、移动或命名规则后，至少运行现有单元测试，并用临时的小型媒体目录验证：分类结果、冲突命名、日志、停止行为和源文件安全性。

## 打包与同步

- `phototidy.spec`、`photodedup.spec` 是 PyInstaller 打包配置；构建产物应生成在本机的 `build/`、`dist/`，不要同步或提交。
- 不同步/提交：`.venv/`、`__pycache__/`、`build/`、`dist/`、`*.exe`、用户媒体、运行日志和用户主目录下的配置文件。
- 编辑前等待 OneDrive 同步完成，避免 macOS 与 Windows 同时改动相同文件。
