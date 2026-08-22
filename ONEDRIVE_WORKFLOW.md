# Windows 与 macOS 共用目录

OneDrive 中的 `Projects/phototidy` 是跨设备共享的项目主目录。

目录内容：

- `*.py`：源代码
- `README*.md`：项目文档
- `requirements.txt`：Python 依赖清单
- `*.spec`：PyInstaller 打包配置
- `.gitignore`：本地生成文件排除规则

不要通过 OneDrive 同步以下内容；它们应在每台电脑上单独生成：

- `.venv/`：Python 虚拟环境
- `__pycache__/`：Python 缓存
- `build/`、`dist/`：打包输出
- `*.exe`：Windows 可执行文件

## Windows

在 VS Code 中打开：

`C:\Users\xieyj\OneDrive\Projects\phototidy`

然后在该目录创建本机虚拟环境并安装依赖。

## macOS

等待 OneDrive 完成同步后，从 Finder 的 OneDrive/Projects 中打开 `phototidy`。
在 Mac 上单独创建虚拟环境；不要复制或复用 Windows 的 `.venv`。

开始编辑前等待 OneDrive 显示同步完成，避免两台设备同时修改同一个文件。
