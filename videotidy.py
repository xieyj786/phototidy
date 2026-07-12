#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VideoTidy —— 视频分类整理工具

功能概述：
    从多层源目录中整理视频文件，按视频拍摄时间 / 文件修改时间归档到目标目录：

    output/
    ├── YYYY年视频文件/
    │   ├── video1.mp4
    │   └── video2.mov
    └── videotidy_log_YYYYMMDD_NNN.txt

    操作模式：
      - 拷贝（安全）：从源目录复制视频文件到目标目录，源文件保留
      - 移动（彻底整理）：从源目录移动视频文件到目标目录；若源目录下某子目录
        因移动而变为空目录，则自动删除该空目录
"""

import os
import queue
import shutil
import threading
from datetime import datetime

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont


# ============================================================
# 常量定义
# ============================================================

DEFAULT_VIDEO_EXTS = {'.mov', '.mp4', '.avi'}
SUPPORTED_MODES = {'copy', 'move'}
LOG_PREFIX = 'videotidy_log'


# ============================================================
# 核心逻辑：视频时间读取
# ============================================================

def get_file_mtime_datetime(filepath):
    """获取文件修改时间，失败时返回当前时间。"""
    try:
        ts = os.path.getmtime(filepath)
    except OSError:
        ts = datetime.now().timestamp()
    return datetime.fromtimestamp(ts)


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
            seconds_since_1904 = int.from_bytes(payload[4:8], 'big')
        elif version == 1:
            if len(payload) < 16:
                payload += f.read(16 - len(payload))
            if len(payload) < 16:
                return None
            seconds_since_1904 = int.from_bytes(payload[4:12], 'big')
        else:
            return None
    except (TypeError, ValueError):
        return None

    return parse_quicktime_datetime(seconds_since_1904)


def get_quicktime_creation_datetime(filepath):
    """尝试读取 .mov/.mp4 文件容器内的创建时间，失败返回 None。"""
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


def get_video_datetime(filepath):
    """
    获取视频归档时间。

    优先级：视频容器内嵌拍摄时间 -> 文件修改时间。

    不使用文件系统创建/生成时间，因为该时间可能在复制或移动后发生变化。
    """
    for dt in (
        get_quicktime_creation_datetime(filepath),
        get_file_mtime_datetime(filepath),
    ):
        if is_reasonable_media_datetime(dt):
            assert dt is not None
            return dt
    return get_file_mtime_datetime(filepath)


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
    """验证源目录的有效性，返回 (valid, error_msg)。"""
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
    """验证目标目录路径是否可创建 / 可写，返回 (valid, error_msg)。"""
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
    """若目标路径已存在同名文件，自动在文件名后追加 _1, _2 ... 避免覆盖。"""
    target_path = os.path.join(target_dir, filename)
    if not os.path.exists(target_path):
        return target_path

    name, ext = os.path.splitext(filename)
    for i in range(1, 10000):
        candidate = os.path.join(target_dir, f"{name}_{i}{ext}")
        if not os.path.exists(candidate):
            return candidate

    from time import time_ns
    timestamp = time_ns() % 1000000
    return os.path.join(target_dir, f"{name}_T{timestamp}{ext}")


def remove_empty_dirs(root_dir):
    """移动模式下，自底向上清理源目录中变为空的子目录。"""
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


def normalize_exts(text):
    """将逗号分隔的后缀字符串规范为小写 .ext 集合。"""
    result = set()
    for item in text.split(','):
        item = item.strip().lower()
        if not item:
            continue
        if not item.startswith('.'):
            item = '.' + item
        result.add(item)
    return result


# ============================================================
# 核心逻辑：日志写入
# ============================================================

def write_log_file(target_dir, stats):
    """写入带日期和递增序号的日志文件，确保已有日志不会被覆盖。"""
    lines = []
    lines.append("=" * 50)
    lines.append("VideoTidy 运行日志")
    lines.append(f"运行时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("=" * 50)
    lines.append("")

    lines.append(f"输入文件总数：{stats['total_files']}")
    lines.append(f"识别为视频的后缀：{', '.join(sorted(stats['video_exts']))}")
    lines.append("")

    lines.append("各后缀文件数：")
    if stats['ext_counts']:
        for ext, count in sorted(stats['ext_counts'].items(), key=lambda x: (-x[1], x[0])):
            ext_name = ext if ext else '(无后缀)'
            lines.append(f"  {ext_name} : {count}")
    else:
        lines.append("  （无）")
    lines.append("")

    lines.append("各年份目录归档视频数：")
    if stats['year_counts']:
        for year_folder, count in sorted(stats['year_counts'].items()):
            lines.append(f"  {year_folder} : {count}")
    else:
        lines.append("  （无）")
    lines.append("")

    lines.append(f"成功归档视频数：{stats['success_count']}")
    lines.append(f"失败文件数：{stats['fail_count']}")
    lines.append(f"跳过文件数（不支持的视频类型）：{stats['skip_count']}")

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
    date_text = datetime.now().strftime('%Y%m%d')
    log_content = '\n'.join(lines)
    sequence = 1
    while True:
        filename = f"{LOG_PREFIX}_{date_text}_{sequence:03d}.txt"
        log_path = os.path.join(target_dir, filename)
        try:
            # 独占创建可避免覆盖已有日志，也能防止多个实例同时取得相同序号。
            with open(log_path, 'x', encoding='utf-8') as f:
                f.write(log_content)
            return log_path
        except FileExistsError:
            sequence += 1


def make_error_stats(error_msg, video_exts=None):
    """生成前置校验失败时的统计结果。"""
    return {
        'total_files': 0,
        'video_exts': set(video_exts or DEFAULT_VIDEO_EXTS),
        'ext_counts': {},
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

def organize_videos(source_dir, target_dir, mode, extra_exts,
                    progress_cb=None, log_cb=None, stop_flag=None):
    """
    执行视频整理任务。

    参数：
        source_dir : 源目录路径
        target_dir : 目标根目录路径
        mode       : 'copy'（拷贝） 或 'move'（移动）
        extra_exts : 额外识别为视频文件的后缀集合
        progress_cb: 回调 (done, total, filepath)，用于更新进度
        log_cb     : 回调 (text)，用于输出处理日志
        stop_flag  : threading.Event，用于中途停止
    """
    from time import time
    start_time = time()
    video_exts = set(DEFAULT_VIDEO_EXTS) | set(extra_exts)

    if mode not in SUPPORTED_MODES:
        error_msg = f"不支持的操作模式：{mode}"
        if log_cb:
            log_cb(f"[错误] {error_msg}")
        return make_error_stats(error_msg, video_exts)

    valid, error_msg = validate_source_dir(source_dir)
    if not valid:
        if log_cb:
            log_cb(f"[错误] {error_msg}")
        return make_error_stats(error_msg, video_exts)

    if is_dir_nested(source_dir, target_dir):
        error_msg = "目标目录不能是源目录的子目录，避免整理自身已处理的内容"
        if log_cb:
            log_cb(f"[错误] {error_msg}")
        return make_error_stats(error_msg, video_exts)

    valid, error_msg = validate_target_dir(target_dir)
    if not valid:
        if log_cb:
            log_cb(f"[错误] {error_msg}")
        return make_error_stats(error_msg, video_exts)

    stats = {
        'total_files': 0,
        'video_exts': video_exts,
        'ext_counts': {},
        'year_counts': {},
        'success_count': 0,
        'fail_count': 0,
        'skip_count': 0,
        'fail_details': [],
        'elapsed_time': 0,
    }

    all_files = []
    for dirpath, dirnames, filenames in os.walk(source_dir):
        for fn in filenames:
            all_files.append(os.path.join(dirpath, fn))

    stats['total_files'] = len(all_files)
    total = len(all_files) or 1

    for idx, filepath in enumerate(all_files):
        if stop_flag is not None and stop_flag.is_set():
            if log_cb:
                log_cb("用户已停止，整理任务中断。")
            break

        ext = os.path.splitext(filepath)[1].lower()
        stats['ext_counts'][ext] = stats['ext_counts'].get(ext, 0) + 1

        if ext not in video_exts:
            stats['skip_count'] += 1
            if log_cb:
                log_cb(f"[跳过] {filepath}（不支持的视频类型）")
            if progress_cb:
                progress_cb(idx + 1, total, filepath)
            continue

        try:
            dt = get_video_datetime(filepath)
            year_folder = f"{dt.year}年视频文件"
            target_subdir = os.path.join(target_dir, year_folder)
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

        except (ValueError, OSError, IOError, shutil.Error) as e:
            stats['fail_count'] += 1
            stats['fail_details'].append(f"{filepath} : {e}")
            if log_cb:
                log_cb(f"[失败] {filepath} : {e}")

        if progress_cb:
            progress_cb(idx + 1, total, filepath)

    if mode == 'move':
        removed_dirs = remove_empty_dirs(source_dir)
        if log_cb:
            for d in removed_dirs:
                log_cb(f"[清理] 删除空目录：{d}")

    stats['elapsed_time'] = time() - start_time
    stats['log_path'] = write_log_file(target_dir, stats)
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


class VideoTidyApp:
    def __init__(self, root):
        self.root = root
        root.title("VideoTidy 视频分类整理工具")
        root.geometry("780x580")
        root.minsize(700, 500)

        self.msg_queue = queue.Queue()
        self.stop_flag = threading.Event()
        self.worker_thread = None

        self._build_ui()
        self.root.after(100, self._poll_queue)

    def _build_ui(self):
        font_family = choose_font_family(self.root)
        FONT = (font_family, 10)
        FONT_BOLD = (font_family, 16, 'bold')
        FONT_SMALL = (font_family, 9)

        tk.Label(self.root, text="VideoTidy 视频分类整理工具", font=FONT_BOLD).pack(pady=(16, 4))
        tk.Label(
            self.root,
            text="按视频拍摄时间或文件修改时间，将视频归档到 YYYY年视频文件 目录",
            font=FONT_SMALL, fg='#666666'
        ).pack(pady=(0, 20))

        frame1 = tk.Frame(self.root)
        frame1.pack(fill='x', padx=16, pady=4)
        tk.Label(frame1, text="1. 源目录：", width=14, anchor='w', font=FONT).pack(side='left')
        self.source_var = tk.StringVar()
        tk.Entry(frame1, textvariable=self.source_var, font=FONT).pack(
            side='left', fill='x', expand=True, padx=(0, 8))
        tk.Button(frame1, text="浏览", width=8, command=self._choose_source).pack(side='left')

        frame2 = tk.Frame(self.root)
        frame2.pack(fill='x', padx=16, pady=4)
        tk.Label(frame2, text="2. 目标根目录：", width=14, anchor='w', font=FONT).pack(side='left')
        self.target_var = tk.StringVar()
        tk.Entry(frame2, textvariable=self.target_var, font=FONT).pack(
            side='left', fill='x', expand=True, padx=(0, 8))
        tk.Button(frame2, text="浏览", width=8, command=self._choose_target).pack(side='left')

        frame3 = tk.Frame(self.root)
        frame3.pack(fill='x', padx=16, pady=8)
        tk.Label(frame3, text="操作模式：", font=FONT).pack(side='left', padx=(0, 8))
        self.mode_var = tk.StringVar(value='copy')
        tk.Radiobutton(frame3, text="拷贝（安全，保留源文件）", variable=self.mode_var,
                       value='copy', font=FONT).pack(side='left', padx=4)
        tk.Radiobutton(frame3, text="移动（彻底整理，清空空目录）", variable=self.mode_var,
                       value='move', font=FONT, fg='#cc4444').pack(side='left', padx=12)

        frame4 = tk.Frame(self.root)
        frame4.pack(fill='x', padx=16, pady=4)
        tk.Label(frame4, text="3. 视频额外后缀：", width=18, anchor='w', font=FONT).pack(side='left')
        self.extra_ext_var = tk.StringVar()
        tk.Entry(frame4, textvariable=self.extra_ext_var, font=FONT).pack(
            side='left', fill='x', expand=True, padx=(0, 8))
        tk.Label(frame4, text="默认 .mov,.mp4,.avi；可添加如 .m4v,.mts", font=FONT_SMALL, fg='#999999').pack(side='left')

        frame5 = tk.Frame(self.root)
        frame5.pack(fill='x', padx=16, pady=(12, 4))
        self.progress = ttk.Progressbar(frame5, orient='horizontal', mode='determinate')
        self.progress.pack(fill='x')

        self.status_var = tk.StringVar(value="就绪 - 请选择源目录和目标根目录")
        tk.Label(self.root, textvariable=self.status_var, fg='#1a6fc4', font=FONT_SMALL).pack(pady=(2, 8))

        btn_frame = tk.Frame(self.root)
        btn_frame.pack(fill='x', padx=16, pady=(0, 8))
        self.start_btn = tk.Button(
            btn_frame, text="开始整理视频", font=(font_family, 13, 'bold'),
            bg='#4caf50', fg='white', activebackground='#43a047',
            height=2, command=self._start
        )
        self.start_btn.pack(side='left', fill='x', expand=True, padx=(0, 6))

        self.stop_btn = tk.Button(
            btn_frame, text="停止", font=(font_family, 13),
            bg='#e0e0e0', height=2, width=8, state='disabled', command=self._stop
        )
        self.stop_btn.pack(side='left')

        log_frame = tk.Frame(self.root)
        log_frame.pack(fill='both', expand=True, padx=16, pady=(0, 16))
        self.log_text = tk.Text(log_frame, font=('Consolas', 9), wrap='none')
        scroll_y = tk.Scrollbar(log_frame, orient='vertical', command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll_y.set)
        self.log_text.pack(side='left', fill='both', expand=True)
        scroll_y.pack(side='right', fill='y')

    def _choose_source(self):
        d = filedialog.askdirectory(title="选择源目录")
        if d:
            self.source_var.set(d)

    def _choose_target(self):
        d = filedialog.askdirectory(title="选择目标根目录")
        if d:
            self.target_var.set(d)

    def _start(self):
        source = self.source_var.get().strip()
        target = self.target_var.get().strip()

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
                "移动模式将把视频从源目录移动到目标目录，\n"
                "并删除源目录下因移动而产生的空子目录。\n\n"
                "此操作不可逆，是否继续？"
            ):
                return

        extra_exts = normalize_exts(self.extra_ext_var.get())

        self.log_text.delete('1.0', 'end')
        self.progress['value'] = 0
        self.start_btn.config(state='disabled', text="整理中...")
        self.stop_btn.config(state='normal')
        self.status_var.set("正在扫描并整理视频，请稍候...")
        self.stop_flag.clear()

        self.worker_thread = threading.Thread(
            target=self._run_worker, args=(source, target, mode, extra_exts), daemon=True
        )
        self.worker_thread.start()

    def _stop(self):
        self.stop_flag.set()
        self.stop_btn.config(state='disabled')
        self.status_var.set("正在停止...")

    def _run_worker(self, source, target, mode, extra_exts):
        def progress_cb(done, total, filepath):
            self.msg_queue.put(('progress', done, total, filepath))

        def log_cb(text):
            self.msg_queue.put(('log', text))

        try:
            stats = organize_videos(
                source, target, mode, extra_exts,
                progress_cb=progress_cb, log_cb=log_cb, stop_flag=self.stop_flag
            )
            self.msg_queue.put(('done', stats))
        except Exception as e:
            self.msg_queue.put(('error', str(e)))

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
                    self.start_btn.config(state='normal', text="开始整理视频")
                    self.stop_btn.config(state='disabled')
                    self.status_var.set(
                        f"完成 - 输入 {stats['total_files']} 个文件，"
                        f"成功 {stats['success_count']}，失败 {stats['fail_count']}，"
                        f"跳过 {stats['skip_count']}"
                    )
                    messagebox.showinfo(
                        "整理完成",
                        f"输入文件总数：{stats['total_files']}\n"
                        f"成功归档视频：{stats['success_count']}\n"
                        f"失败：{stats['fail_count']}\n"
                        f"跳过（不支持的视频类型）：{stats['skip_count']}\n\n"
                        f"详细日志已写入目标目录下的 "
                        f"{os.path.basename(stats.get('log_path', ''))}"
                    )

                elif kind == 'error':
                    self.start_btn.config(state='normal', text="开始整理视频")
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
    VideoTidyApp(root)
    root.mainloop()


if __name__ == '__main__':
    main()
