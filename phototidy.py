#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PhotoTidy —— 照片分类整理工具

功能概述：
    从杂乱的源目录中整理出真实拍摄的照片 / 视频，以及截图、转发图片等其他图片，
    按拍摄时间（或修改时间）与文件类型，分类归档到目标目录：

    photo-tidy/
    ├── YYYY年照片集/
    │   ├── 视频文件/          # .mov / .mp4 等视频文件
    │   ├── 相机型号拍摄照片/   # 根据 EXIF Model 生成，如 DSC-RX100M3拍摄照片
    │   ├── 1-2月照片/         # 拍摄时间在1、2月的照片
    │   ├── 3-4月照片/
    │   ├── 5-6月照片/
    │   ├── 7-8月照片/
    │   ├── 9-10月照片/
    │   ├── 11-12月照片/
    │   └── 其他图片文件/       # 截图、转发图片、无拍摄时间的图片等
    └── phototidy_log.txt      # 运行日志

    操作模式：
      - 拷贝（安全）：从源目录复制文件到目标目录，源文件保留
      - 移动（彻底整理）：从源目录移动文件到目标目录；若源目录下某子目录
        因移动而变为空目录，则自动删除该空目录
"""

import os
import re
import shutil
import sys
import threading
import queue
import warnings
from datetime import datetime

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont

try:
    from PIL import Image
    _PIL_IMPORT_ERROR = None
except ImportError as e:
    Image = None
    _PIL_IMPORT_ERROR = e


# 可选：HEIC / HEIF 支持（若安装了 pillow-heif，则可读取其 EXIF 拍摄时间）
try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
    _HEIF_SUPPORT = True
except ImportError:
    _HEIF_SUPPORT = False



# ============================================================
# 常量定义
# ============================================================

VIDEO_EXTS = {'.mov', '.mp4', '.avi'}
PHOTO_EXTS = {'.jpg', '.jpeg', '.heic', '.heif'}
DEFAULT_OTHER_IMAGE_EXTS = {'.png', '.bmp', '.gif', '.tiff', '.tif', '.webp'}
CAMERA_PHOTO_MIN_COUNT = 10

# 月份 -> 目标子目录名
MONTH_TO_FOLDER = {
    1: '1-2月照片', 2: '1-2月照片',
    3: '3-4月照片', 4: '3-4月照片',
    5: '5-6月照片', 6: '5-6月照片',
    7: '7-8月照片', 8: '7-8月照片',
    9: '9-10月照片', 10: '9-10月照片',
    11: '11-12月照片', 12: '11-12月照片',
}

# EXIF 中可能包含拍摄时间的标签：DateTimeOriginal, DateTimeDigitized, DateTime
EXIF_DATETIME_TAGS = (36867, 36868, 306)
EXIF_IFD_TAG = 0x8769  # Exif SubIFD
EXIF_MAKE_TAG = 271
EXIF_MODEL_TAG = 272

MOBILE_DEVICE_KEYWORDS = (
    'IPHONE', 'IPAD', 'IPOD', 'ANDROID', 'SAMSUNG', 'GALAXY', 'GT-', 'SM-',
    'PIXEL', 'HUAWEI', 'HONOR', 'XIAOMI', 'REDMI', 'MI ', 'OPPO', 'VIVO',
    'ONEPLUS', 'MOTOROLA', 'MOTO ', 'NOKIA', 'LENOVO', 'MEIZU', 'REALME',
)
CAMERA_MAKE_KEYWORDS = (
    'CANON', 'NIKON', 'SONY', 'FUJIFILM', 'FUJI', 'OLYMPUS', 'OM DIGITAL',
    'PANASONIC', 'LEICA', 'PENTAX', 'RICOH', 'CASIO', 'KODAK', 'HASSELBLAD',
    'SIGMA', 'DJI', 'GOPRO',
)
CAMERA_MODEL_PREFIXES = (
    'DSC-', 'ILCE-', 'ILCA-', 'NEX-', 'SLT-', 'ZV-', 'NIKON ', 'COOLPIX',
    'CANON ', 'EOS ', 'POWERSHOT', 'IXUS', 'FINEPIX', 'X-T', 'X-E', 'X-PRO',
    'X100', 'DMC-', 'DC-', 'LUMIX', 'PENTAX ', 'GR ', 'GOPRO',
)
SUPPORTED_MODES = {'copy', 'move'}


def is_pillow_available():
    """检查 Pillow 是否可用。"""
    return Image is not None


def get_missing_pillow_message():
    """返回适合当前环境的 Pillow 安装提示。"""
    executable = sys.executable or 'python3'
    return (
        "缺少必要依赖 Pillow，无法读取图片文件。\n\n"
        f"请在当前环境中执行：\n{executable} -m pip install -r requirements.txt"
    )


# ============================================================
# 核心逻辑：EXIF 时间读取
# ============================================================

def parse_exif_datetime(value):
    """解析形如 '2023:08:15 12:30:00' 的 EXIF 时间字符串，失败返回 None"""
    if not value:
        return None
    value = str(value).strip()
    m = re.match(r'^(\d{4})[:\-](\d{2})[:\-](\d{2})[ T](\d{2}):(\d{2}):(\d{2})', value)
    if not m:
        return None
    try:
        y, mo, d, h, mi, s = map(int, m.groups())
        return datetime(y, mo, d, h, mi, s)
    except ValueError:
        return None


def get_exif_datetime(filepath):
    """尝试读取图片的 EXIF 原始拍摄时间（DateTimeOriginal 等），失败返回 None"""
    if Image is None:
        return None
    try:
        # 抑制 PIL 对截断/损坏图片文件的警告（如 "Truncated File Read"）
        with warnings.catch_warnings():
            warnings.filterwarnings('ignore', category=UserWarning, module='PIL')
            img = Image.open(filepath)
        with img:
            exif = img.getexif()

            if not exif:
                return None

            candidates = []

            # 优先从 Exif SubIFD 中读取 DateTimeOriginal / DateTimeDigitized
            try:
                exif_ifd = exif.get_ifd(EXIF_IFD_TAG)
                for tag in EXIF_DATETIME_TAGS:
                    if tag in exif_ifd:
                        candidates.append(exif_ifd[tag])
            except (AttributeError, KeyError, IndexError):
                pass

            # 再尝试主 IFD 中的 DateTime 等字段
            for tag in EXIF_DATETIME_TAGS:
                if tag in exif:
                    candidates.append(exif[tag])

            for value in candidates:
                dt = parse_exif_datetime(value)
                if dt is not None:
                    return dt
    except (IOError, OSError, ValueError):
        return None
    return None


def get_exif_camera_make_model(filepath):
    """尝试读取图片 EXIF 中的相机厂商与型号，失败返回 ('', '')。"""
    if Image is None:
        return '', ''
    try:
        with warnings.catch_warnings():
            warnings.filterwarnings('ignore', category=UserWarning, module='PIL')
            img = Image.open(filepath)
        with img:
            exif = img.getexif()
            if not exif:
                return '', ''
            make = str(exif.get(EXIF_MAKE_TAG, '')).strip()
            model = str(exif.get(EXIF_MODEL_TAG, '')).strip()
            return make, model
    except (IOError, OSError, ValueError):
        return '', ''


def is_standalone_camera(make, model):
    """严格识别独立照相机，排除手机和平板的 EXIF 型号。"""
    make_upper = make.upper()
    model_upper = model.upper()
    combined = f"{make_upper} {model_upper}"

    if not model_upper:
        return False
    if any(keyword in combined for keyword in MOBILE_DEVICE_KEYWORDS):
        return False
    return (
        any(keyword in make_upper for keyword in CAMERA_MAKE_KEYWORDS)
        or model_upper.startswith(CAMERA_MODEL_PREFIXES)
    )


def sanitize_folder_name(name):
    """清理 Windows 目录名中的非法字符。"""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip(' .')
    return name or '未知型号'


def get_camera_photo_folder(model):
    """根据 EXIF 相机型号生成目标子目录名。"""
    return f"{sanitize_folder_name(model)}拍摄照片"


def get_file_mtime_datetime(filepath):
    """获取文件修改时间，失败时返回当前时间。"""
    try:
        ts = os.path.getmtime(filepath)
    except OSError:
        ts = datetime.now().timestamp()
    return datetime.fromtimestamp(ts)


def get_file_birth_datetime(filepath):
    """获取文件系统记录的生成时间 / 创建时间，失败返回 None。"""
    try:
        stat_result = os.stat(filepath)
    except OSError:
        return None

    birth_ts = getattr(stat_result, 'st_birthtime', None)
    if birth_ts is None:
        return None

    try:
        return datetime.fromtimestamp(birth_ts)
    except (OSError, OverflowError, ValueError):
        return None


def is_reasonable_media_datetime(dt):
    """过滤明显无效的媒体时间，如 QuickTime 默认纪元或异常未来时间。"""
    if dt is None:
        return False
    current_year = datetime.now().year
    return 1970 <= dt.year <= current_year + 1


def parse_quicktime_datetime(seconds_since_1904):
    """解析 QuickTime/MP4 中以 1904-01-01 为起点的创建时间。"""
    try:
        unix_seconds = int(seconds_since_1904) - 2082844800
        dt = datetime.fromtimestamp(unix_seconds)
    except (OSError, OverflowError, ValueError):
        return None
    return dt if is_reasonable_media_datetime(dt) else None


def read_atom_header(f):
    """读取 MP4/MOV atom 头，返回 (atom_type, payload_start, atom_end)。"""
    start = f.tell()
    header = f.read(8)
    if len(header) < 8:
        return None

    size = int.from_bytes(header[:4], 'big')
    atom_type = header[4:8]
    header_size = 8

    if size == 1:
        large_size_bytes = f.read(8)
        if len(large_size_bytes) < 8:
            return None
        size = int.from_bytes(large_size_bytes, 'big')
        header_size = 16
    elif size == 0:
        try:
            current = f.tell()
            f.seek(0, os.SEEK_END)
            size = f.tell() - start
            f.seek(current)
        except OSError:
            return None

    if size < header_size:
        return None

    payload_start = start + header_size
    atom_end = start + size
    return atom_type, payload_start, atom_end


def read_mvhd_creation_datetime(f, atom_end):
    """读取 mvhd atom 中的 creation_time。"""
    payload = f.read(min(20, max(0, atom_end - f.tell())))
    if len(payload) < 8:
        return None

    version = payload[0]
    try:
        if version == 0:
            if len(payload) < 8:
                return None
            seconds_since_1904 = int.from_bytes(payload[4:8], 'big')
        elif version == 1:
            if len(payload) < 16:
                more = f.read(16 - len(payload))
                payload += more
            if len(payload) < 16:
                return None
            seconds_since_1904 = int.from_bytes(payload[4:12], 'big')
        else:
            return None
    except (TypeError, ValueError):
        return None

    return parse_quicktime_datetime(seconds_since_1904)


def get_quicktime_creation_datetime(filepath):
    """尝试读取 .mov/.mp4/.m4v 等文件容器内的创建时间，失败返回 None。"""
    try:
        with open(filepath, 'rb') as f:
            f.seek(0, os.SEEK_END)
            file_end = f.tell()
            f.seek(0)

            while f.tell() < file_end:
                header = read_atom_header(f)
                if header is None:
                    return None
                atom_type, payload_start, atom_end = header
                if atom_end > file_end:
                    return None

                if atom_type == b'moov':
                    f.seek(payload_start)
                    while f.tell() < atom_end:
                        child_header = read_atom_header(f)
                        if child_header is None:
                            return None
                        child_type, child_payload_start, child_end = child_header
                        if child_end > atom_end:
                            return None
                        if child_type == b'mvhd':
                            f.seek(child_payload_start)
                            return read_mvhd_creation_datetime(f, child_end)
                        f.seek(child_end)
                    return None

                f.seek(atom_end)
    except (OSError, ValueError):
        return None
    return None


def get_video_datetime(filepath) -> datetime:
    """
    获取视频归档时间。

    优先级：视频容器创建时间 -> 文件系统生成时间 -> 文件修改时间。
    始终返回一个有效的 datetime 对象（最后回退到文件修改时间）。
    """
    for dt in (
        get_quicktime_creation_datetime(filepath),
        get_file_birth_datetime(filepath),
        get_file_mtime_datetime(filepath),
    ):
        if is_reasonable_media_datetime(dt):
            assert dt is not None
            return dt
    return get_file_mtime_datetime(filepath)


def get_mtime_year(filepath):
    """获取文件修改时间对应的年份"""
    return get_file_mtime_datetime(filepath).year


# ============================================================
# 核心逻辑：分类
# ============================================================

def classify_file(filepath, ext, other_image_exts, camera_photo_counts=None):
    """
    根据文件后缀与 EXIF 拍摄时间确定文件分类。

    返回 (category, year, detail):
        category: 'video' | 'camera_photo' | 'photo' | 'other' | None（None 表示不处理该文件）
        year:     归档所属年份（int）
        detail:   拍摄月份，或 category == 'camera_photo' 时的相机型号目录名
    """
    if ext in VIDEO_EXTS:
        # 视频文件：按视频创建时间 / 文件生成时间 / 修改时间归入年份目录 -> 视频文件/
        dt = get_video_datetime(filepath)
        return 'video', dt.year, None

    if ext in PHOTO_EXTS:
        dt = get_exif_datetime(filepath)
        if dt is not None:
            make, model = get_exif_camera_make_model(filepath)
            if is_standalone_camera(make, model):
                camera_folder = get_camera_photo_folder(model)
                count_key = (dt.year, camera_folder)
                if camera_photo_counts and camera_photo_counts.get(count_key, 0) >= CAMERA_PHOTO_MIN_COUNT:
                    return 'camera_photo', dt.year, camera_folder
            # 有 EXIF 拍摄时间 -> 按拍摄时间的年/月归档
            return 'photo', dt.year, dt.month
        # 没有 / 无法解析拍摄时间 -> 其他图片文件
        return 'other', get_mtime_year(filepath), None

    if ext in other_image_exts:
        # png/bmp 等其他类型图片文件，或用户自定义的额外后缀
        return 'other', get_mtime_year(filepath), None

    # 其他类型文件（非视频、非图片）不处理
    return None, None, None


# ============================================================
# 核心逻辑：文件操作辅助函数
# ============================================================

def is_dir_nested(parent, child):
    """
    检查 child 目录是否嵌套在 parent 目录内。
    返回 True 如果 child 是 parent 的子目录，否则返回 False。
    """
    try:
        parent_abs = os.path.normcase(os.path.realpath(parent))
        child_abs = os.path.normcase(os.path.realpath(child))
        return os.path.commonpath([parent_abs, child_abs]) == parent_abs
    except (TypeError, ValueError, OSError):
        return False


def validate_source_dir(source_dir):
    """
    验证源目录的有效性。
    返回 (valid, error_msg) 元组。
    """
    if not source_dir or not isinstance(source_dir, str):
        return False, "源目录路径不能为空或非字符串"
    
    try:
        source_dir = source_dir.strip()
        if not os.path.isdir(source_dir):
            return False, f"源目录不存在或不是有效目录：{source_dir}"
        if not os.access(source_dir, os.R_OK):
            return False, f"源目录无读取权限：{source_dir}"
        return True, ""
    except (TypeError, OSError) as e:
        return False, f"源目录验证失败：{e}"


def validate_target_dir(target_dir):
    """
    验证目标目录路径是否可创建 / 可写。
    返回 (valid, error_msg) 元组。
    """
    if not target_dir or not isinstance(target_dir, str):
        return False, "目标目录路径不能为空或非字符串"

    try:
        target_dir = target_dir.strip()
        os.makedirs(target_dir, exist_ok=True)
        if not os.path.isdir(target_dir):
            return False, f"目标路径不是有效目录：{target_dir}"
        if not os.access(target_dir, os.W_OK):
            return False, f"目标目录无写入权限：{target_dir}"
        return True, ""
    except (TypeError, OSError) as e:
        return False, f"目标目录验证失败：{e}"


def get_unique_target_path(target_dir, filename):
    """
    若目标路径已存在同名文件，自动在文件名后追加 _1, _2 ... 避免覆盖。
    为避免过度递增（_1, _2, _3...），限制最多尝试 9999 次。
    """
    target_path = os.path.join(target_dir, filename)
    if not os.path.exists(target_path):
        return target_path

    name, ext = os.path.splitext(filename)
    # 限制尝试次数，防止无限循环
    for i in range(1, 10000):
        candidate = os.path.join(target_dir, f"{name}_{i}{ext}")
        if not os.path.exists(candidate):
            return candidate
    
    # 如果尝试了 10000 个都不行，使用时间戳作为后缀
    from time import time_ns
    timestamp = time_ns() % 1000000  # 取最后 6 位
    return os.path.join(target_dir, f"{name}_T{timestamp}{ext}")


def remove_empty_dirs(root_dir):
    """
    移动模式下，自底向上清理源目录中变为空的子目录
    （不删除 root_dir 本身）。返回被删除的目录列表。
    """
    removed = []
    root_abs = os.path.abspath(root_dir)
    for dirpath, dirnames, filenames in os.walk(root_dir, topdown=False):
        if os.path.abspath(dirpath) == root_abs:
            continue
        try:
            if not os.listdir(dirpath):
                os.rmdir(dirpath)
                removed.append(dirpath)
        except OSError:
            pass
    return removed


# ============================================================
# 核心逻辑：日志写入
# ============================================================

def write_log_file(target_dir, stats):
    """将本次运行的统计信息写入 target_dir/phototidy_log.txt"""
    log_path = os.path.join(target_dir, 'phototidy_log.txt')
    lines = []
    lines.append("=" * 50)
    lines.append("PhotoTidy 运行日志")
    lines.append(f"运行时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("=" * 50)
    lines.append("")

    lines.append(f"输入文件总数：{stats['total_files']}")
    lines.append("")

    lines.append("各后缀文件数：")
    if stats['ext_counts']:
        for ext, count in sorted(stats['ext_counts'].items(), key=lambda x: (-x[1], x[0])):
            ext_name = ext if ext else '(无后缀)'
            lines.append(f"  {ext_name} : {count}")
    else:
        lines.append("  （无）")
    lines.append("")

    lines.append(f"有可用 EXIF 拍摄时间的文件总数：{stats['exif_time_count']}")
    lines.append(f"独立相机拍摄照片总数：{stats.get('standalone_camera_photo_count', 0)}")
    lines.append("")

    lines.append("各年份目录归档文件数：")
    if stats['year_counts']:
        for year, count in sorted(stats['year_counts'].items()):
            lines.append(f"  {year} : {count}")
    else:
        lines.append("  （无）")
    lines.append("")

    lines.append(f"成功归档文件数：{stats['success_count']}")
    lines.append(f"失败文件数：{stats['fail_count']}")
    lines.append(f"跳过文件数（不支持的文件类型）：{stats['skip_count']}")
    
    # 添加耗时信息
    elapsed = stats.get('elapsed_time', 0)
    if elapsed > 0:
        minutes, seconds = divmod(int(elapsed), 60)
        lines.append(f"处理耗时：{minutes}分{seconds}秒")
    lines.append("")

    lines.append("失败明细：")
    if stats['fail_details']:
        for d in stats['fail_details']:
            lines.append(f"  {d}")
    else:
        lines.append("  无")
    lines.append("")

    os.makedirs(target_dir, exist_ok=True)
    with open(log_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))

    return log_path


def make_error_stats(error_msg):
    """生成前置校验失败时的统计结果。"""
    return {
        'total_files': 0,
        'ext_counts': {},
        'exif_time_count': 0,
        'standalone_camera_photo_count': 0,
        'year_counts': {},
        'success_count': 0,
        'fail_count': 1,
        'skip_count': 0,
        'fail_details': [error_msg],
        'elapsed_time': 0,
    }


# ============================================================
# 核心逻辑：整理主流程
# ============================================================

def organize_files(source_dir, target_dir, mode, extra_exts,
                    progress_cb=None, log_cb=None, stop_flag=None):
    """
    执行整理任务。

    参数：
        source_dir : 源目录路径
        target_dir : 目标根目录路径
        mode       : 'copy'（拷贝） 或 'move'（移动）
        extra_exts : 额外识别为“其他图片文件”的后缀集合，如 {'.gif', '.webp'}
        progress_cb: 回调 (done, total, filepath)，用于更新进度
        log_cb     : 回调 (text)，用于输出处理日志
        stop_flag  : threading.Event，用于中途停止

    返回：统计信息字典 stats
    """
    # 记录开始时间
    from time import time
    start_time = time()

    if not is_pillow_available():
        error_msg = get_missing_pillow_message()
        if log_cb:
            log_cb(f"[错误] {error_msg}")
        return make_error_stats(error_msg)

    if mode not in SUPPORTED_MODES:
        error_msg = f"不支持的操作模式：{mode}"
        if log_cb:
            log_cb(f"[错误] {error_msg}")
        return make_error_stats(error_msg)
    
    # 参数验证
    valid, error_msg = validate_source_dir(source_dir)
    if not valid:
        if log_cb:
            log_cb(f"[错误] {error_msg}")
        return make_error_stats(error_msg)

    # 检查源目录和目标目录是否嵌套
    if is_dir_nested(source_dir, target_dir):
        error_msg = "目标目录不能是源目录的子目录，避免整理自身已处理的内容"
        if log_cb:
            log_cb(f"[错误] {error_msg}")
        return make_error_stats(error_msg)

    valid, error_msg = validate_target_dir(target_dir)
    if not valid:
        if log_cb:
            log_cb(f"[错误] {error_msg}")
        return make_error_stats(error_msg)
    
    other_image_exts = set(DEFAULT_OTHER_IMAGE_EXTS) | set(extra_exts)

    stats = {
        'total_files': 0,
        'ext_counts': {},
        'exif_time_count': 0,
        'standalone_camera_photo_count': 0,
        'year_counts': {},
        'success_count': 0,
        'fail_count': 0,
        'skip_count': 0,
        'fail_details': [],
        'elapsed_time': 0,
    }

    # 先扫描出所有文件，便于显示总进度
    all_files = []
    for dirpath, dirnames, filenames in os.walk(source_dir):
        for fn in filenames:
            all_files.append(os.path.join(dirpath, fn))

    stats['total_files'] = len(all_files)
    total = len(all_files) or 1

    camera_photo_counts = {}
    for filepath in all_files:
        ext = os.path.splitext(filepath)[1].lower()
        if ext not in PHOTO_EXTS:
            continue
        dt = get_exif_datetime(filepath)
        if dt is None:
            continue
        make, model = get_exif_camera_make_model(filepath)
        if not is_standalone_camera(make, model):
            continue
        camera_folder = get_camera_photo_folder(model)
        count_key = (dt.year, camera_folder)
        camera_photo_counts[count_key] = camera_photo_counts.get(count_key, 0) + 1
    stats['standalone_camera_photo_count'] = sum(camera_photo_counts.values())

    for idx, filepath in enumerate(all_files):
        if stop_flag is not None and stop_flag.is_set():
            if log_cb:
                log_cb("用户已停止，整理任务中断。")
            break

        ext = os.path.splitext(filepath)[1].lower()
        stats['ext_counts'][ext] = stats['ext_counts'].get(ext, 0) + 1

        try:
            category, year, detail = classify_file(filepath, ext, other_image_exts, camera_photo_counts)
        except (ValueError, OSError, IOError) as e:
            stats['fail_count'] += 1
            stats['fail_details'].append(f"{filepath} : 分类时出错 - {e}")
            if log_cb:
                log_cb(f"[失败] {filepath} : 分类时出错 - {e}")
            if progress_cb:
                progress_cb(idx + 1, total, filepath)
            continue

        if category is None:
            stats['skip_count'] += 1
            if log_cb:
                log_cb(f"[跳过] {filepath}（不支持的文件类型）")
            if progress_cb:
                progress_cb(idx + 1, total, filepath)
            continue

        if category in ('photo', 'camera_photo'):
            stats['exif_time_count'] += 1

        year_folder = f"{year}年照片集"
        if category == 'video':
            sub_folder: str = '视频文件'
        elif category == 'camera_photo':
            sub_folder = str(detail) if detail else '其他图片文件'
        elif category == 'photo' and isinstance(detail, int):
            sub_folder = MONTH_TO_FOLDER[detail]
        else:
            sub_folder = '其他图片文件'

        target_subdir = os.path.join(target_dir, year_folder, sub_folder)

        try:
            os.makedirs(target_subdir, exist_ok=True)
            target_path = get_unique_target_path(target_subdir, os.path.basename(filepath))

            if mode == 'copy':
                shutil.copy2(filepath, target_path)
            else:
                shutil.move(filepath, target_path)

            stats['success_count'] += 1
            stats['year_counts'][year_folder] = stats['year_counts'].get(year_folder, 0) + 1

            if log_cb:
                action = '拷贝' if mode == 'copy' else '移动'
                log_cb(f"[{action}] {filepath}\n      -> {target_path}")

        except (OSError, IOError, shutil.Error) as e:
            stats['fail_count'] += 1
            stats['fail_details'].append(f"{filepath} : {e}")
            if log_cb:
                log_cb(f"[失败] {filepath} : {e}")

        if progress_cb:
            progress_cb(idx + 1, total, filepath)

    # 移动模式：清理源目录中产生的空子目录
    if mode == 'move':
        removed_dirs = remove_empty_dirs(source_dir)
        if log_cb:
            for d in removed_dirs:
                log_cb(f"[清理] 删除空目录：{d}")

    # 计算需耗时间
    stats['elapsed_time'] = time() - start_time
    
    write_log_file(target_dir, stats)
    return stats


# ============================================================
# 图形界面
# ============================================================

def choose_font_family(root):
    """在不同操作系统上选择可用的中文界面字体。"""
    preferred_fonts = (
        'Microsoft YaHei UI',
        'PingFang SC',
        'Hiragino Sans GB',
        'Noto Sans CJK SC',
        'WenQuanYi Micro Hei',
        'Arial Unicode MS',
    )
    try:
        available = set(tkfont.families(root))
    except tk.TclError:
        available = set()
    for family in preferred_fonts:
        if family in available:
            return family
    return tkfont.nametofont('TkDefaultFont').actual('family')


class PhotoTidyApp:
    def __init__(self, root):
        self.root = root
        root.title("PhotoTidy 照片分类整理工具")
        root.geometry("780x580")
        root.minsize(700, 500)

        self.msg_queue = queue.Queue()
        self.stop_flag = threading.Event()
        self.worker_thread = None

        self._build_ui()
        self.root.after(100, self._poll_queue)

    # ---------------------------------------------------
    def _build_ui(self):
        font_family = choose_font_family(self.root)
        FONT = (font_family, 10)
        FONT_BOLD = (font_family, 16, 'bold')
        FONT_SMALL = (font_family, 9)

        # 标题
        tk.Label(self.root, text="PhotoTidy 照片分类整理工具", font=FONT_BOLD).pack(pady=(16, 4))
        tk.Label(
            self.root,
            text="按拍摄时间将照片 / 视频归档到目标目录，截图与其他图片归入“其他图片文件”",
            font=FONT_SMALL, fg='#666666'
        ).pack(pady=(0, 12))

        # HEIC/HEIF 支持状态
        if not is_pillow_available():
            heif_status = "Pillow 未安装，图片读取功能不可用"
            heif_color = '#c62828'
        elif _HEIF_SUPPORT:
            heif_status = "HEIC/HEIF 支持已启用"
            heif_color = '#2e7d32'
        else:
            heif_status = "HEIC/HEIF 支持未安装（pip install pillow-heif）"
            heif_color = '#c62828'
        tk.Label(
            self.root, text=heif_status,
            font=FONT_SMALL, fg=heif_color
        ).pack(pady=(0, 8))


        # 1. 源目录
        frame1 = tk.Frame(self.root)
        frame1.pack(fill='x', padx=16, pady=4)
        tk.Label(frame1, text="1. 源目录：", width=14, anchor='w', font=FONT).pack(side='left')
        self.source_var = tk.StringVar()
        tk.Entry(frame1, textvariable=self.source_var, font=FONT).pack(
            side='left', fill='x', expand=True, padx=(0, 8))
        tk.Button(frame1, text="浏览", width=8, command=self._choose_source).pack(side='left')

        # 2. 目标根目录
        frame2 = tk.Frame(self.root)
        frame2.pack(fill='x', padx=16, pady=4)
        tk.Label(frame2, text="2. 目标根目录：", width=14, anchor='w', font=FONT).pack(side='left')
        self.target_var = tk.StringVar()
        tk.Entry(frame2, textvariable=self.target_var, font=FONT).pack(
            side='left', fill='x', expand=True, padx=(0, 8))
        tk.Button(frame2, text="浏览", width=8, command=self._choose_target).pack(side='left')

        # 操作模式
        frame3 = tk.Frame(self.root)
        frame3.pack(fill='x', padx=16, pady=8)
        tk.Label(frame3, text="操作模式：", font=FONT).pack(side='left', padx=(0, 8))
        self.mode_var = tk.StringVar(value='copy')
        tk.Radiobutton(frame3, text="拷贝（安全，保留源文件）", variable=self.mode_var,
                        value='copy', font=FONT).pack(side='left', padx=4)
        tk.Radiobutton(frame3, text="移动（彻底整理，清空空目录）", variable=self.mode_var,
                        value='move', font=FONT, fg='#cc4444').pack(side='left', padx=12)

        # 3. 其他图片额外后缀
        frame4 = tk.Frame(self.root)
        frame4.pack(fill='x', padx=16, pady=4)
        tk.Label(frame4, text="3. 其他图片额外后缀：", width=18, anchor='w', font=FONT).pack(side='left')
        self.extra_ext_var = tk.StringVar()
        tk.Entry(frame4, textvariable=self.extra_ext_var, font=FONT).pack(
            side='left', fill='x', expand=True, padx=(0, 8))
        tk.Label(frame4, text="（用逗号分隔，如 .gif,.webp）", font=FONT_SMALL, fg='#999999').pack(side='left')

        # 进度条
        frame5 = tk.Frame(self.root)
        frame5.pack(fill='x', padx=16, pady=(12, 4))
        self.progress = ttk.Progressbar(frame5, orient='horizontal', mode='determinate')
        self.progress.pack(fill='x')

        # 状态行
        self.status_var = tk.StringVar(value="就绪 - 请选择源目录和目标根目录")
        tk.Label(self.root, textvariable=self.status_var, fg='#1a6fc4', font=FONT_SMALL).pack(pady=(2, 8))

        # 操作按钮
        btn_frame = tk.Frame(self.root)
        btn_frame.pack(fill='x', padx=16, pady=(0, 8))
        self.start_btn = tk.Button(
            btn_frame, text="开始整理文件", font=(font_family, 13, 'bold'),
            bg='#4caf50', fg='white', activebackground='#43a047',
            height=2, command=self._start
        )
        self.start_btn.pack(side='left', fill='x', expand=True, padx=(0, 6))

        self.stop_btn = tk.Button(
            btn_frame, text="停止", font=(font_family, 13),
            bg='#e0e0e0', height=2, width=8, state='disabled', command=self._stop
        )
        self.stop_btn.pack(side='left')

        # 日志输出
        log_frame = tk.Frame(self.root)
        log_frame.pack(fill='both', expand=True, padx=16, pady=(0, 16))
        self.log_text = tk.Text(log_frame, font=('Consolas', 9), wrap='none')
        scroll_y = tk.Scrollbar(log_frame, orient='vertical', command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll_y.set)
        self.log_text.pack(side='left', fill='both', expand=True)
        scroll_y.pack(side='right', fill='y')

    # ---------------------------------------------------
    def _choose_source(self):
        d = filedialog.askdirectory(title="选择源目录")
        if d:
            self.source_var.set(d)

    def _choose_target(self):
        d = filedialog.askdirectory(title="选择目标根目录")
        if d:
            self.target_var.set(d)

    # ---------------------------------------------------
    def _start(self):
        source = self.source_var.get().strip()
        target = self.target_var.get().strip()

        if not is_pillow_available():
            messagebox.showerror("缺少依赖", get_missing_pillow_message())
            return

        valid, error_msg = validate_source_dir(source)
        if not valid:
            messagebox.showerror("错误", error_msg)
            return

        if is_dir_nested(source, target):
            messagebox.showerror("错误", "目标目录不能是源目录的子目录，避免整理自身已处理的内容")
            return

        valid, error_msg = validate_target_dir(target)
        if not valid:
            messagebox.showerror("错误", error_msg)
            return

        mode = self.mode_var.get()
        if mode not in SUPPORTED_MODES:
            messagebox.showerror("错误", f"不支持的操作模式：{mode}")
            return
        if mode == 'move':
            if not messagebox.askyesno(
                "确认",
                "移动模式将把文件从源目录移动到目标目录，\n"
                "并删除源目录下因移动而产生的空子目录。\n\n"
                "此操作不可逆，是否继续？"
            ):
                return

        # 解析额外后缀
        extra_exts = set()
        for item in self.extra_ext_var.get().split(','):
            item = item.strip().lower()
            if not item:
                continue
            if not item.startswith('.'):
                item = '.' + item
            extra_exts.add(item)

        self.log_text.delete('1.0', 'end')
        self.progress['value'] = 0
        self.start_btn.config(state='disabled', text="整理中...")
        self.stop_btn.config(state='normal')
        self.status_var.set("正在扫描并整理文件，请稍候...")
        self.stop_flag.clear()

        self.worker_thread = threading.Thread(
            target=self._run_worker, args=(source, target, mode, extra_exts), daemon=True
        )
        self.worker_thread.start()

    def _stop(self):
        self.stop_flag.set()
        self.stop_btn.config(state='disabled')
        self.status_var.set("正在停止...")

    # ---------------------------------------------------
    def _run_worker(self, source, target, mode, extra_exts):
        def progress_cb(done, total, filepath):
            self.msg_queue.put(('progress', done, total, filepath))

        def log_cb(text):
            self.msg_queue.put(('log', text))

        try:
            stats = organize_files(
                source, target, mode, extra_exts,
                progress_cb=progress_cb, log_cb=log_cb, stop_flag=self.stop_flag
            )
            self.msg_queue.put(('done', stats))
        except Exception as e:
            self.msg_queue.put(('error', str(e)))

    # ---------------------------------------------------
    def _poll_queue(self):
        try:
            while True:
                item = self.msg_queue.get_nowait()
                kind = item[0]

                if kind == 'progress':
                    _, done, total, filepath = item
                    self.progress['maximum'] = total
                    self.progress['value'] = done
                    self.status_var.set(f"处理中 ({done}/{total})：{os.path.basename(filepath)}")

                elif kind == 'log':
                    self.log_text.insert('end', item[1] + '\n')
                    self.log_text.see('end')

                elif kind == 'done':
                    stats = item[1]
                    self.start_btn.config(state='normal', text="开始整理文件")
                    self.stop_btn.config(state='disabled')
                    self.status_var.set(
                        f"完成 - 输入 {stats['total_files']} 个文件，"
                        f"成功 {stats['success_count']}，失败 {stats['fail_count']}，"
                        f"跳过 {stats['skip_count']}"
                    )
                    messagebox.showinfo(
                        "整理完成",
                        f"输入文件总数：{stats['total_files']}\n"
                        f"有拍摄时间的文件数：{stats['exif_time_count']}\n"
                        f"成功归档：{stats['success_count']}\n"
                        f"失败：{stats['fail_count']}\n"
                        f"跳过（不支持的类型）：{stats['skip_count']}\n\n"
                        f"详细日志已写入目标目录下的 phototidy_log.txt"
                    )

                elif kind == 'error':
                    self.start_btn.config(state='normal', text="开始整理文件")
                    self.stop_btn.config(state='disabled')
                    self.status_var.set("发生错误")
                    messagebox.showerror("错误", item[1])

        except queue.Empty:
            pass

        self.root.after(100, self._poll_queue)


# ============================================================
# 程序入口
# ============================================================

def main():
    root = tk.Tk()
    PhotoTidyApp(root)
    root.mainloop()


if __name__ == '__main__':
    main()
