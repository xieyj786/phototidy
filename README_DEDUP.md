# PhotoDedup Technical Documentation

English | [中文](#photodedup-技术文档)

`photodedup.py` is a local Tkinter-based photo de-duplication tool. It is designed to clean up an already organized photo library. It does not delete duplicates directly; instead, duplicate files are moved into the `重复图片文件/` folder under the selected library.

## Usage

```bash
python3 -m pip install -r requirements.txt
python3 photodedup.py
```

Dependencies:

- Pillow is used to read images and EXIF data, and to compute dHash and grayscale histograms.
- `pillow-heif` registers HEIC/HEIF support. Without it, the app can still start, but HEIC/HEIF EXIF reading may be limited.
- dHash is implemented in this project with Pillow; the third-party `imagehash` package is not required.

## Target Directory Layout

PhotoDedup processes each top-level subdirectory independently. It never compares files across different top-level folders.

```text
PhotoLibrary/
├── 2023年照片集/
├── 2024年照片集/
├── Travel/
├── 重复图片文件/
│   ├── 2023年照片集/
│   ├── 2024年照片集/
│   └── Travel/
└── photodedup_log_YYYYMMDD_NNN.txt
```

For example, if a duplicate is found inside `2024年照片集/`, it is moved to:

```text
PhotoLibrary/重复图片文件/2024年照片集/
```

The `重复图片文件/` folder itself is excluded from scanning.

## Supported Formats

Scanned image extensions:

- `.jpg`
- `.jpeg`
- `.heic`
- `.heif`

Perceptual visual de-duplication mainly targets JPEG files. HEIC/HEIF files participate in global MD5 checks and capture-time grouping.

## Date Detection

For each image, PhotoDedup tries to determine capture time:

1. Prefer EXIF `DateTimeOriginal`, `DateTimeDigitized`, and `DateTime`.
2. If EXIF time is unavailable, try common filename timestamps:
   - `YYYYMMDD_HHMMSS`
   - `YYYY-MM-DD HH-MM-SS`
   - similar `_` and `-` separated variants
3. `real_exif_dt` records only real EXIF time. Filename time is used only for grouping and duplicate-copy detection.

## Overall Workflow

1. Start the GUI and load the last library path and threshold from `~/.photodedup_config.json`.
2. Select the photo library directory and dHash threshold.
3. List all top-level subdirectories, excluding `重复图片文件/`.
4. Recursively scan supported images in each top-level subdirectory.
5. Run de-duplication stages:
   - Global MD5 exact duplicate detection
   - Same-second capture-time duplicate detection
   - JPEG perceptual hash duplicate detection
6. Move duplicate files to `重复图片文件/<TopLevelFolder>/`.
7. Write `photodedup_log_YYYYMMDD_NNN.txt`; runs on the same day increment the three-digit sequence.

## Algorithms

### 1. Global MD5 Exact Duplicates

Within the same top-level subdirectory, all supported images that have not already been moved are hashed with MD5. Files with identical MD5 values are considered byte-for-byte duplicates:

- Keep the largest file.
- Move the others into the corresponding duplicate folder.
- This stage does not depend on capture time.

### 2. Same-Second Capture-Time Deduplication

Images with capture time are grouped by:

```text
(capture time to second precision, extension group)
```

Extension groups:

- `jpeg`: `.jpg/.jpeg`
- `heic`: `.heic/.heif`

Two checks run inside each group:

1. Filename timestamp copies: if normalized filename timestamps match, they are treated as likely duplicate-copy names such as `(1)` or `_1`; the largest file is kept.
2. JPEG visual similarity: same-second JPEGs are compared with dHash, then confirmed with grayscale histogram correlation. Similar files keep the largest version.

The same-second visual check uses more permissive internal candidate thresholds:

- `SAME_TIME_DHASH_THRESHOLD = 8`
- `SAME_TIME_HIST_CONFIRM_THRESHOLD = 0.90`

The UI dHash threshold is still used first for strict matching.

### 3. JPEG Perceptual Hash Deduplication

This stage processes only remaining `.jpg/.jpeg` files.

It has two phases:

1. JPEG files without real EXIF capture time are compared with each other. Similar files are clustered, and the largest file is kept.
2. JPEG files with real EXIF capture time are compared against the remaining JPEGs without real EXIF time. If similar, the file with real EXIF time is kept.

JPEG files with real EXIF capture time are not compared against each other in this stage, reducing cross-time false positives.

## dHash and Histogram Confirmation

dHash implementation:

1. Open the image with Pillow and apply EXIF orientation correction.
2. Convert it to grayscale.
3. Resize it to `(hash_size + 1) x hash_size`, defaulting to `9 x 8`.
4. Compare adjacent pixels row by row to produce a 64-bit integer fingerprint.
5. Use Hamming distance to measure visual similarity.

Histogram confirmation:

- Resize the image to a `256 x 256` grayscale image.
- Build a 64-bin grayscale histogram.
- Compute Pearson correlation.
- The normal confirmation threshold is `HIST_CORR_CONFIRM_THRESHOLD = 0.98`.

The histogram is only a confirmation step after a dHash hit. It does not trigger duplicate detection by itself.

## Performance Optimizations

To avoid full pairwise comparisons in large libraries, the code includes several optimizations:

- Compute dHash only when missing, and use `ThreadPoolExecutor` for larger batches.
- Use full pairwise comparison for small sets where it is simple and reliable.
- Use dHash chunk indexes for small thresholds.
- Use a BK-tree candidate search for larger thresholds and larger file sets.
- Cache file size, MD5, dHash, and histogram values in each file info dictionary.

## Threshold

The UI field "感知哈希相似度阈值" is the dHash Hamming-distance threshold. Its range is `0` to `3`:

- `0`: strictest; only identical dHash values match.
- `1`: default and conservative, recommended for first use.
- `2` to `3`: more permissive; may find more similar images but increases false-positive risk.

It is best to test the threshold on a copied or backup library before running it on a production photo library.

## Log

The log is written to the library root and includes:

- Runtime
- dHash threshold
- Number of scanned top-level subdirectories
- Total supported images
- Total duplicates
- Per-folder image counts and duplicate counts by stage
- Duplicate file, kept file, and moved destination relationships
- Error details
- Elapsed time

## Safety Notes

- PhotoDedup moves duplicate files; the original path no longer contains them.
- Duplicate files are not deleted and can be manually restored from `重复图片文件/`.
- The program does not compare across top-level folders, preserving separation between years, albums, or themes.
- The UI stop button interrupts remaining work, but completed moves are not rolled back.

---

# PhotoDedup 技术文档

`photodedup.py` 是一个基于 Tkinter 的照片查重整理工具，用于对已经整理好的图片库执行重复文件清理。程序不会删除重复文件，而是把判定为重复的文件移动到图片库下的 `重复图片文件/` 目录。

## 运行方式

```bash
python3 -m pip install -r requirements.txt
python3 photodedup.py
```

依赖说明：

- Pillow 用于读取图片、EXIF、计算 dHash 和直方图。
- `pillow-heif` 用于注册 HEIC/HEIF 读取支持；未安装时程序仍可启动，但 HEIC/HEIF 的 EXIF 读取能力会受限。
- dHash 由项目代码自行实现，不依赖第三方 `imagehash` 包。

## 适用目录

PhotoDedup 以图片库下的一级子目录为单位独立查重，不会跨一级子目录比较。

```text
图片库/
├── 2023年照片集/
├── 2024年照片集/
├── 旅行照片/
├── 重复图片文件/
│   ├── 2023年照片集/
│   ├── 2024年照片集/
│   └── 旅行照片/
└── photodedup_log_YYYYMMDD_NNN.txt
```

例如，在 `2024年照片集/` 中发现重复文件时，重复文件会被移动到：

```text
图片库/重复图片文件/2024年照片集/
```

`重复图片文件/` 本身会被排除，不会再次参与扫描。

## 支持格式

查重扫描的图片后缀：

- `.jpg`
- `.jpeg`
- `.heic`
- `.heif`

其中感知哈希视觉查重主要针对 JPEG；HEIC/HEIF 参与全局 MD5 和拍摄时间分组。

## 时间读取规则

程序会为每个图片读取拍摄时间：

1. 优先读取 EXIF `DateTimeOriginal`、`DateTimeDigitized`、`DateTime`。
2. 如果 EXIF 时间不可用，尝试从常见文件名时间戳解析：
   - `YYYYMMDD_HHMMSS`
   - `YYYY-MM-DD HH-MM-SS`
   - 同类 `_`、`-` 分隔格式
3. `real_exif_dt` 只记录真实 EXIF 时间；文件名时间只用于补充分组和识别副本。

## 总体流程

1. 启动界面，读取用户目录 `~/.photodedup_config.json` 中保存的上次图片库路径和阈值。
2. 用户选择图片库目录和 dHash 阈值。
3. 程序列出图片库下所有一级子目录，排除 `重复图片文件/`。
4. 对每个一级子目录递归扫描支持格式图片。
5. 依次执行：
   - 全局 MD5 完全重复查重
   - 同秒拍摄组内重复查重
   - JPEG 感知哈希查重
6. 重复文件移动到 `重复图片文件/<一级子目录名>/`。
7. 写入 `photodedup_log_YYYYMMDD_NNN.txt`；同一天多次运行时递增三位序号。

## 查重算法

### 1. 全局 MD5 完全重复

同一个一级子目录内，所有尚未移除的支持格式图片都会计算 MD5。MD5 完全一致时判定为字节级重复：

- 保留文件体积最大的一个。
- 其他文件移动到对应重复目录。
- 该步骤不依赖拍摄时间。

### 2. 同秒拍摄组内查重

对有拍摄时间的图片，按以下 key 分组：

```text
(拍摄时间精确到秒, 扩展名组)
```

扩展名组分为：

- `jpeg`：`.jpg/.jpeg`
- `heic`：`.heic/.heif`

同组内执行两类判断：

1. 文件名时间戳副本：如果文件名中解析出的规范化时间戳相同，通常代表 `(1)`、`_1` 等副本命名，保留体积最大者。
2. JPEG 视觉相似：对同秒 JPEG 计算 dHash，并用灰度直方图相关系数辅助确认，相似时保留体积最大者。

同秒视觉判断使用更宽松的内部候选阈值：

- `SAME_TIME_DHASH_THRESHOLD = 8`
- `SAME_TIME_HIST_CONFIRM_THRESHOLD = 0.90`

但仍会优先使用界面设置的 dHash 阈值进行严格判断。

### 3. JPEG 感知哈希查重

该步骤只处理尚未移除的 `.jpg/.jpeg` 文件。

分两阶段：

1. 无真实 EXIF 拍摄时间的 JPEG 之间互相比较，相似文件聚类后保留体积最大者。
2. 有真实 EXIF 拍摄时间的 JPEG 与剩余无真实 EXIF 时间的 JPEG 比较。相似时优先保留有真实 EXIF 时间的文件。

有真实 EXIF 拍摄时间的 JPEG 之间不会在该阶段互相比较，避免跨时间误判。

## dHash 与直方图

dHash 实现方式：

1. 用 Pillow 打开图片并应用 EXIF 方向校正。
2. 转为灰度图。
3. 缩放为 `(hash_size + 1) x hash_size`，默认 `9 x 8`。
4. 逐行比较相邻像素亮度，得到 64 位整数指纹。
5. 用汉明距离衡量两张图的结构相似度。

直方图确认：

- 图片缩放到 `256 x 256` 灰度图。
- 生成 64 bins 灰度直方图。
- 计算 Pearson 相关系数。
- 普通感知哈希确认阈值为 `HIST_CORR_CONFIRM_THRESHOLD = 0.98`。

直方图只作为 dHash 命中后的辅助确认，不会单独触发判重。

## 性能优化

为了避免大图库全量两两比较，代码包含几类优化：

- dHash 缺失时才计算，并使用 `ThreadPoolExecutor` 并行处理较大批量。
- 小集合直接全量比较，结果简单可靠。
- 阈值较小时使用 dHash 分块索引生成候选对。
- 阈值较大且文件数较多时使用 BK-tree 生成候选对。
- 文件大小、MD5、dHash、直方图会缓存在文件信息字典中，避免重复计算。

## 阈值说明

界面中的“感知哈希相似度阈值”是 dHash 汉明距离阈值，范围为 `0` 到 `3`：

- `0`：最严格，只接受 dHash 完全一致。
- `1`：默认值，较保守，适合首次使用。
- `2` 到 `3`：更宽松，可发现更多相似图，也会提高误判风险。

建议先用测试目录或备份目录验证阈值，再处理正式图片库。

## 运行日志

日志写入图片库根目录，内容包括：

- 运行时间
- 使用的 dHash 阈值
- 扫描的一级子目录数量
- 支持图片总数
- 重复文件总数
- 每个一级子目录的图片数、各查重阶段重复数
- 重复文件、保留文件、移动目标之间的对应关系
- 错误明细
- 程序运行时长

## 安全注意事项

- PhotoDedup 会移动重复文件，源位置不再保留该文件。
- 重复文件不会被删除，可从 `重复图片文件/` 手动恢复。
- 程序不会跨一级子目录查重，适合保留不同相册、年份或主题目录之间的独立性。
- 界面支持停止任务；已经移动的文件不会自动回滚。

---

# VideoDedup Technical Documentation

English | [中文](#videodedup-技术文档)

`videodedup.py` is a local Tkinter-based exact video de-duplication tool. It scans an entire video library, detects byte-for-byte duplicate files, and moves redundant copies into `重复视频文件/`. It does not use perceptual similarity and does not delete files directly.

## Usage

```bash
python3 videodedup.py
```

Only the Python standard library is required. No FFmpeg, ffprobe, Pillow, or third-party hashing package is used.

## Target Directory Layout

Unlike PhotoDedup, VideoDedup compares supported files across all nested folders, including different year folders:

```text
VideoLibrary/
├── 2024年视频文件/
├── 2025年视频文件/
├── 重复视频文件/
│   ├── duplicate1.mp4
│   └── duplicate2.mov
└── videodedup_log_YYYYMMDD_NNN.txt
```

The duplicate directory is excluded from future scans. Moved duplicates are stored flat; their original directory hierarchy is not preserved. Filename conflicts are resolved by adding `_1`, `_2`, and so on.

## Supported Formats

The default extensions are `.mp4`, `.mov`, and `.avi`. The GUI accepts additional comma-, Chinese-comma-, or space-separated extensions such as `.mkv,.m4v,.mts`. Extensions only select candidate files; file contents are not validated as playable video.

## Exact-Duplicate Algorithm

1. Recursively collect supported files from the whole library, excluding `重复视频文件/`.
2. Group files by byte size. A file whose size is unique is skipped without hashing.
3. Read same-size candidates in 8 MiB chunks and calculate SHA-256.
4. Group by `(file size, SHA-256)`. Only files matching both values are duplicates.
5. Within each duplicate group, keep the file with the earliest filesystem modification time.
6. Move every other file into the flat duplicate directory and record the relationship in the log.

Capture time and modification time do not determine whether content is duplicated. Modification time is used only to choose which identical copy remains.

## Progress, Stop, and Configuration

- The progress bar counts only same-size hash candidates, not every scanned video.
- Pressing Stop interrupts further hashing. Duplicate groups formed from hashes already completed are still processed and moved; completed moves are not rolled back.
- The last library path and extension text are stored in `~/.videodedup_config.json`.

## Log

Logs use `videodedup_log_YYYYMMDD_NNN.txt`, for example `videodedup_log_20260712_001.txt`. They include runtime, extensions, algorithm, scanned count, duplicate count, SHA-256 values, kept/original/moved paths, errors recorded by the main workflow, elapsed time, and stopped status.

## Review Findings and Safety Notes

- Python syntax compilation succeeds.
- Size plus SHA-256 equality is a conservative exact-duplicate rule with negligible practical collision risk, but it will not find transcoded, resized, clipped, or metadata-modified versions of the same video.
- Duplicate moves are irreversible from the GUI, although files remain recoverable from `重复视频文件/`; use a backup or copied library first.
- Move failures are shown in the live GUI and included in the final log's error list.
- Logs are created in exclusive mode, so concurrent instances cannot overwrite the same numbered log.
- If a modification time cannot be read, that file sorts last instead of aborting the remaining de-duplication pass.

---

# VideoDedup 技术文档

`videodedup.py` 是一个基于 Tkinter 的精确视频查重工具。它会扫描整个视频库，通过文件内容判断完全重复文件，并把多余副本移动到 `重复视频文件/`。程序不进行感知相似查重，也不会直接删除文件。

## 运行方式

```bash
python3 videodedup.py
```

程序仅使用 Python 标准库，不需要 FFmpeg、ffprobe、Pillow 或第三方哈希包。

## 适用目录

与 PhotoDedup 不同，VideoDedup 会跨所有下级目录比较，包括不同年份目录：

```text
视频库/
├── 2024年视频文件/
├── 2025年视频文件/
├── 重复视频文件/
│   ├── duplicate1.mp4
│   └── duplicate2.mov
└── videodedup_log_YYYYMMDD_NNN.txt
```

`重复视频文件/` 会被排除，不参与后续扫描。重复文件采用平铺方式存放，不保留原目录层级；同名冲突时追加 `_1`、`_2` 等序号。

## 支持格式

默认扩展名为 `.mp4`、`.mov`、`.avi`。界面允许用英文逗号、中文逗号或空格补充 `.mkv,.m4v,.mts` 等后缀。扩展名只用于筛选候选文件，程序不会验证文件是否能够正常播放。

## 精确查重算法

1. 递归扫描整个视频库，排除 `重复视频文件/`。
2. 按字节大小分组；大小唯一的文件不计算哈希。
3. 对相同大小的候选文件以 8 MiB 分块读取，计算 SHA-256。
4. 按 `(文件大小, SHA-256)` 分组，两项完全一致才判定为重复。
5. 每个重复组保留文件系统修改时间最早的一份。
6. 其余文件移动到平铺的重复目录，并在日志中记录对应关系。

拍摄时间和修改时间不参与重复内容判断；修改时间只用于决定完全相同的副本中保留哪一份。

## 进度、停止与配置

- 进度条统计相同大小、需要计算哈希的候选文件数，而不是扫描到的全部视频数。
- 点击“停止”后不再计算新的哈希，但已完成哈希形成的重复组仍会继续处理和移动；已经完成的移动不会回滚。
- 上次使用的视频库路径和扩展名保存在 `~/.videodedup_config.json`。

## 运行日志

日志使用 `videodedup_log_YYYYMMDD_NNN.txt`，例如 `videodedup_log_20260712_001.txt`。内容包括运行时间、处理后缀、判重方式、扫描数量、重复数量、SHA-256、保留/原始/移动目标路径、主流程记录的错误、运行时长和停止状态。

## 代码检查结论与安全注意事项

- Python 语法编译检查通过。
- “大小 + SHA-256 完全一致”是保守可靠的精确查重规则，实际哈希碰撞风险可忽略；但无法识别转码、裁剪、缩放或仅元数据不同的同内容视频。
- GUI 中的移动操作不可撤销，但文件仍保留在 `重复视频文件/`，建议先使用备份或复制的视频库测试。
- 单个文件移动失败时会同时显示在界面实时日志中，并写入最终日志的错误列表。
- 日志采用独占创建模式，多个实例同时运行也不会覆盖同一个编号日志。
- 读取修改时间失败时，该文件会排在组内最后，不会中断剩余查重流程。
