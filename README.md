# PhotoTidy / VideoTidy Technical Documentation

English | [中文](#phototidy--videotidy-技术文档)

This project contains two local Tkinter-based desktop tools for organizing media files:

- `phototidy.py`: organizes photos, videos, and other images into a year/type/month based photo library.
- `videotidy.py`: organizes video files only, grouped by year.

Both tools provide a graphical interface, progress bar, runtime log, stop button, copy/move modes, and automatic filename de-duplication by appending suffixes such as `_1`, `_2`, and so on.

## Requirements

```bash
python3 -m pip install -r requirements.txt
python3 phototidy.py
python3 videotidy.py
```

Notes:

- Tkinter is part of the Python standard library on most Python installations.
- `phototidy.py` requires Pillow to read image EXIF metadata.
- HEIC/HEIF support depends on `pillow-heif`. Without it, the app can still start, but HEIC/HEIF EXIF reading may be limited.
- `videotidy.py` only uses the Python standard library.

## PhotoTidy

`phototidy.py` recursively scans a source directory and archives files into a target root directory according to file type, capture time, and camera model.

### Output Structure

```text
TargetRoot/
├── YYYY年照片集/
│   ├── 视频文件/
│   ├── CameraModel拍摄照片/
│   ├── 1-2月照片/
│   ├── 3-4月照片/
│   ├── 5-6月照片/
│   ├── 7-8月照片/
│   ├── 9-10月照片/
│   ├── 11-12月照片/
│   └── 其他图片文件/
└── phototidy_log.txt
```

Directories are created on demand. If a category receives no files in a run, that category directory is not created.

### Supported File Types

Default types:

- Videos: `.mov`, `.mp4`, `.avi`
- Captured photos: `.jpg`, `.jpeg`, `.heic`, `.heif`
- Other images: `.png`, `.bmp`, `.gif`, `.tiff`, `.tif`, `.webp`

The UI field for extra image extensions can add more file types, separated by commas, for example `.avif,.svg`.

### Date Detection

Photo dates:

1. For `.jpg/.jpeg/.heic/.heif`, PhotoTidy first reads EXIF `DateTimeOriginal`, `DateTimeDigitized`, and `DateTime`.
2. If EXIF parsing succeeds, the file is placed under `YYYY年照片集/`, then into the corresponding two-month folder.
3. Photos without usable EXIF capture time are placed under `其他图片文件/`, using file modification time for the year.

Video dates:

1. Prefer QuickTime creation time from the MOV/MP4 `moov/mvhd` atom.
2. Fall back to filesystem birth time, `st_birthtime`.
3. Fall back again to file modification time.
4. Obviously invalid media dates are ignored. Valid years are limited to `1970` through the current year plus one.

### Standalone Camera Folder

PhotoTidy reads EXIF `Make` and `Model`, identifies standalone cameras by keyword rules, and excludes phones and tablets. If a camera model reaches `CAMERA_PHOTO_MIN_COUNT = 10` photos in the same year, those photos are archived into:

```text
YYYY年照片集/CameraModel拍摄照片/
```

Example:

```text
2024年照片集/DSC-RX100M3拍摄照片/
```

Standalone camera photos below the threshold are still grouped by month.

### Workflow

1. Validate that the source directory exists and is readable, and that the target directory can be created and written.
2. Reject targets nested inside the source directory.
3. Recursively scan all files and collect extension statistics.
4. Pre-scan photo EXIF data to count standalone camera photos by year and model.
5. Classify each file:
   - Videos go to `YYYY年照片集/视频文件/`
   - Standalone camera photos above the threshold go to `YYYY年照片集/<CameraModel>拍摄照片/`
   - Ordinary photos with EXIF capture time go to the corresponding two-month folder
   - Photos without capture time and other images go to `其他图片文件/`
   - Unsupported files are skipped
6. Copy with `shutil.copy2` or move with `shutil.move`.
7. In move mode, remove empty source subdirectories from bottom to top.
8. Write `phototidy_log.txt`.

### Modes

- Copy: keeps source files and preserves metadata with `shutil.copy2`.
- Move: moves files out of the source directory and removes empty subdirectories. The UI asks for confirmation.

### Log

`phototidy_log.txt` includes:

- Total input files
- File counts by extension
- Number of files with usable EXIF capture time
- Number of standalone camera photos
- Archived file counts by year
- Success, failure, and skipped counts
- Failure details
- Elapsed time

## VideoTidy

`videotidy.py` is a lightweight video-only organizer. It does not read image EXIF metadata.

### Output Structure

```text
TargetRoot/
├── YYYY年视频文件/
│   ├── video1.mp4
│   └── video2.mov
└── videotidy_log.txt
```

### Supported File Types

By default, VideoTidy recognizes `.mov`, `.mp4`, and `.avi`. Extra video extensions can be added in the UI, for example `.m4v,.mts`.

### Workflow

1. Validate source directory, target directory, and mode.
2. Reject targets nested inside the source directory.
3. Recursively scan all files.
4. For supported videos, determine archive time from:
   - MOV/MP4 QuickTime creation time
   - Filesystem birth time
   - File modification time
5. Archive videos into `YYYY年视频文件/`.
6. Copy or move files according to the selected mode.
7. In move mode, remove empty source subdirectories.
8. Write `videotidy_log.txt`.

### Log

`videotidy_log.txt` includes:

- Total input files
- Video extensions recognized in the run
- File counts by extension
- Archived video counts by year
- Success, failure, and skipped counts
- Failure details
- Elapsed time

## Safety

- The target directory cannot be inside the source directory.
- Existing files are never overwritten; a unique target path is generated automatically.
- Move mode requires confirmation.
- The UI stop button interrupts remaining work, but completed file operations are not rolled back.

---

# PhotoTidy / VideoTidy 技术文档

本项目包含两个基于 Tkinter 的本地文件整理工具：

- `phototidy.py`：整理照片、视频和其他图片，输出到按年份、类型、月份划分的照片库。
- `videotidy.py`：只整理视频文件，输出到按年份划分的视频目录。

两个工具都支持图形界面、进度条、运行日志、停止任务、拷贝/移动两种模式，并会在同名文件冲突时自动追加 `_1`、`_2` 等后缀避免覆盖。

## 运行环境

```bash
python3 -m pip install -r requirements.txt
python3 phototidy.py
python3 videotidy.py
```

说明：

- Tkinter 来自 Python 标准库，通常无需通过 `pip` 安装。
- `phototidy.py` 需要 Pillow 读取图片 EXIF。
- HEIC/HEIF 支持依赖 `pillow-heif`；未安装时程序仍可启动，但 HEIC/HEIF 的 EXIF 读取能力会受限。
- `videotidy.py` 仅使用标准库，不依赖 Pillow。

## PhotoTidy

`phototidy.py` 用于从多层源目录中扫描文件，并根据文件类型、拍摄时间、相机型号归档到目标根目录。

### 输出结构

```text
目标根目录/
├── YYYY年照片集/
│   ├── 视频文件/
│   ├── 相机型号拍摄照片/
│   ├── 1-2月照片/
│   ├── 3-4月照片/
│   ├── 5-6月照片/
│   ├── 7-8月照片/
│   ├── 9-10月照片/
│   ├── 11-12月照片/
│   └── 其他图片文件/
└── phototidy_log.txt
```

目录按需创建。某类文件本次没有成功归档时，不会创建对应子目录。

### 支持的文件类型

默认识别：

- 视频：`.mov`、`.mp4`、`.avi`
- 拍摄照片：`.jpg`、`.jpeg`、`.heic`、`.heif`
- 其他图片：`.png`、`.bmp`、`.gif`、`.tiff`、`.tif`、`.webp`

界面中的“其他图片额外后缀”可继续补充扩展名，多个后缀用逗号分隔，例如 `.avif,.svg`。

### 时间读取规则

照片时间：

1. 对 `.jpg/.jpeg/.heic/.heif`，优先读取 EXIF 中的 `DateTimeOriginal`、`DateTimeDigitized`、`DateTime`。
2. 解析成功后按 EXIF 年份进入 `YYYY年照片集/`，再按月份进入双月目录。
3. 没有可用 EXIF 拍摄时间的照片进入 `其他图片文件/`，年份使用文件修改时间。

视频时间：

1. 优先读取 MOV/MP4 容器 `moov/mvhd` atom 中的 QuickTime creation time。
2. 若容器时间不可用，使用文件系统创建时间 `st_birthtime`。
3. 若仍不可用，使用文件修改时间。
4. 明显异常的时间会被过滤，只接受 `1970` 到当前年份后一年的范围。

### 独立相机照片目录

PhotoTidy 会读取 EXIF `Make` 和 `Model`，用关键词识别独立相机，并排除手机、平板等设备。某一年中同一相机型号照片数量达到 `CAMERA_PHOTO_MIN_COUNT = 10` 后，会归档到：

```text
YYYY年照片集/相机型号拍摄照片/
```

例如：

```text
2024年照片集/DSC-RX100M3拍摄照片/
```

未达到数量阈值的独立相机照片仍按月份归档。

### 分类流程

1. 校验源目录存在且可读，目标目录可创建且可写。
2. 拒绝目标目录位于源目录内部，避免重复整理自身输出。
3. 递归扫描源目录下全部文件，统计总数和后缀数量。
4. 预扫描照片 EXIF，统计每年每个独立相机型号的照片数量。
5. 逐个文件分类：
   - 视频进入 `YYYY年照片集/视频文件/`
   - 达到阈值的独立相机照片进入 `YYYY年照片集/<相机型号>拍摄照片/`
   - 有 EXIF 拍摄时间的普通照片进入对应双月目录
   - 无拍摄时间的照片和其他图片进入 `其他图片文件/`
   - 非支持类型计为跳过
6. 根据模式执行 `copy2` 或 `move`。
7. 移动模式结束后，自底向上删除源目录中因移动产生的空子目录。
8. 写入 `phototidy_log.txt`。

### 操作模式

- 拷贝：保留源文件，使用 `shutil.copy2` 保留文件元数据。
- 移动：使用 `shutil.move` 移走源文件，并清理空目录。界面会二次确认。

### 运行日志

`phototidy_log.txt` 包含：

- 输入文件总数
- 各后缀文件数
- 有可用 EXIF 拍摄时间的文件数
- 独立相机拍摄照片总数
- 各年份目录归档文件数
- 成功、失败、跳过数量
- 失败明细
- 处理耗时

## VideoTidy

`videotidy.py` 是更轻量的视频专用整理工具，只处理视频文件，不读取图片 EXIF。

### 输出结构

```text
目标根目录/
├── YYYY年视频文件/
│   ├── video1.mp4
│   └── video2.mov
└── videotidy_log.txt
```

### 支持的文件类型

默认识别 `.mov`、`.mp4`、`.avi`。界面中的“视频额外后缀”可以补充更多视频扩展名，例如 `.m4v,.mts`。

### 整理流程

1. 校验源目录、目标目录和操作模式。
2. 拒绝目标目录位于源目录内部。
3. 递归扫描源目录下全部文件。
4. 对支持的视频文件读取归档时间：
   - MOV/MP4 QuickTime creation time
   - 文件系统创建时间
   - 文件修改时间
5. 将视频归档到 `YYYY年视频文件/`。
6. 根据模式执行拷贝或移动。
7. 移动模式下删除源目录中的空子目录。
8. 写入 `videotidy_log.txt`。

### 运行日志

`videotidy_log.txt` 包含：

- 输入文件总数
- 本次识别为视频的后缀集合
- 各后缀文件数
- 各年份目录归档视频数
- 成功、失败、跳过数量
- 失败明细
- 处理耗时

## 安全策略

- 目标目录不能是源目录的子目录。
- 同名文件不会覆盖，会自动生成唯一目标路径。
- 移动模式会弹窗确认。
- 支持在界面中点击“停止”中断后续处理；已完成的文件操作不会回滚。
