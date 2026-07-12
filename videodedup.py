#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VideoDedup —— 视频查重整理工具

功能概述：
    对视频文件集目录（如按年份分类的 视频文件集/YYYY年视频文件/ 结构）进行整体查重。
    与照片不同，视频文件没有稳定可靠的"感知哈希"判重手段，因此查重只采用最严格、
    最可靠的判定方式：

        1. 文件大小不同 -> 一定不是同一文件，直接跳过，不计算哈希，节省时间；
        2. 文件大小相同 -> 计算 SHA-256 哈希值，哈希完全一致才确认是同一文件内容。

    拍摄时间 / 文件修改时间不作为判重依据（仅记录在日志中供参考），因为这两个时间
    在复制、传输、不同文件系统之间会发生变化，不可靠。

    查重针对整个视频库目录（不区分年份子目录，跨年份的重复文件也能识别出来），
    重复文件统一移动到：

        视频文件集/
        ├── YYYY年视频文件/
        ├── ...
        ├── 重复视频文件/          # 查出的重复文件（平铺存放）
        └── videodedup_log_20260712_001.txt   # 运行日志（日期+三位序号命名）

    每个重复组保留修改时间最早的一份，其余移动到「重复视频文件」目录，并在日志中
    记录完整对应关系。
"""

import os
import shutil
import threading
import queue
import hashlib
import time
from datetime import datetime

import tkinter as tk
from tkinter import filedialog, messagebox, ttk


# ============================================================
# 常量定义
# ============================================================

DEFAULT_EXTENSIONS_TEXT = ".mp4,.mov,.avi"
DUP_DIR_NAME = "重复视频文件"

HASH_CHUNK_SIZE = 8 * 1024 * 1024  # 8MB，分块读取，适合大视频文件

# 配置文件：保存在用户主目录下，跨次运行记忆上次目录与扩展名设置
CONFIG_PATH = os.path.join(os.path.expanduser('~'), '.videodedup_config.json')


# ============================================================
# 配置读写
# ============================================================

def load_config():
    """读取配置文件，返回字典；文件不存在或解析失败时返回空字典"""
    import json
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(data):
    """将字典写入配置文件"""
    import json
    try:
        with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ============================================================
# 工具函数
# ============================================================

def human_size(num_bytes):
    """把字节数转换成易读的字符串，如 1.20GB"""
    size = float(num_bytes)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if size < 1024.0:
            return f"{size:.2f}{unit}"
        size /= 1024.0
    return f"{size:.2f}PB"


def parse_extensions(text):
    """
    解析界面上输入的扩展名文本（逗号/中文逗号/空格分隔），
    规范化为小写、带前导点的集合，例如 ".mp4,.mov, avi" -> {'.mp4', '.mov', '.avi'}
    """
    if not text:
        return set()
    raw = text.replace('，', ',').replace(' ', ',')
    result = set()
    for item in raw.split(','):
        item = item.strip().lower()
        if not item:
            continue
        if not item.startswith('.'):
            item = '.' + item
        result.add(item)
    return result


def compute_sha256(filepath, chunk_size=HASH_CHUNK_SIZE):
    """计算文件的 SHA-256 哈希值（分块读取，适合大视频文件）"""
    h = hashlib.sha256()
    with open(filepath, 'rb') as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def get_mtime_str(filepath):
    try:
        ts = os.path.getmtime(filepath)
        return datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S')
    except OSError:
        return '未知'


def get_mtime_for_sort(filepath):
    """获取用于排序的修改时间；读取失败时排到最后，避免中断整轮查重。"""
    try:
        return os.path.getmtime(filepath)
    except OSError:
        return float('inf')


def get_unique_target_path(dup_dir, filename):
    """
    如果重复目录下已存在同名文件，则在文件名后加序号，避免覆盖。
    例如 IMG_0001.MOV -> IMG_0001_1.MOV
    """
    base, ext = os.path.splitext(filename)
    candidate = os.path.join(dup_dir, filename)
    counter = 1
    while os.path.exists(candidate):
        candidate = os.path.join(dup_dir, f"{base}_{counter}{ext}")
        counter += 1
    return candidate


def move_to_duplicate_dir(filepath, dup_dir):
    """将重复文件移动到「重复视频文件」目录（平铺存放），返回目标路径"""
    os.makedirs(dup_dir, exist_ok=True)
    target_path = get_unique_target_path(dup_dir, os.path.basename(filepath))
    shutil.move(filepath, target_path)
    return target_path


# ============================================================
# 核心逻辑：扫描视频文件
# ============================================================

def collect_video_files(library_dir, extensions):
    """
    递归收集 library_dir 下所有指定扩展名的视频文件路径，
    跳过「重复视频文件」目录本身（避免把已移动的文件重复扫描）。
    """
    video_files = []
    skip_dir_abs = os.path.abspath(os.path.join(library_dir, DUP_DIR_NAME))

    for dirpath, dirnames, filenames in os.walk(library_dir):
        dirpath_abs = os.path.abspath(dirpath)
        if dirpath_abs == skip_dir_abs or dirpath_abs.startswith(skip_dir_abs + os.sep):
            dirnames[:] = []
            continue

        for filename in filenames:
            ext = os.path.splitext(filename)[1].lower()
            if ext in extensions:
                video_files.append(os.path.join(dirpath, filename))

    return video_files


# ============================================================
# 核心逻辑：大小 + SHA-256 哈希查重
# ============================================================

def video_dedup(video_files, dup_dir, progress_cb=None, log_cb=None, stop_flag=None):
    """
    对 video_files 列表按"大小 -> SHA-256哈希"两级分组查重。
    每组重复文件保留修改时间最早的一份，其余移动到 dup_dir。

    返回：(dup_count, dup_relations, stopped, errors)
        dup_relations: 每项为字典 {duplicate, kept, size, sha256, moved_to}
        stopped: 是否被用户中途停止
    """
    dup_count = 0
    dup_relations = []
    stopped = False
    errors = []

    # 第一步：按文件大小分组，大小唯一的文件不可能重复，直接排除
    size_groups = {}
    for filepath in video_files:
        try:
            size = os.path.getsize(filepath)
        except OSError as e:
            if log_cb:
                log_cb(f"[警告] 无法读取文件大小，跳过：{filepath}（{e}）")
            continue
        size_groups.setdefault(size, []).append(filepath)

    candidates = []
    for size, files in size_groups.items():
        if len(files) > 1:
            candidates.extend(files)

    total = len(candidates) or 1
    if log_cb:
        log_cb(f"文件大小重复的候选文件共 {len(candidates)} 个，开始计算 SHA-256 哈希...")

    # 第二步：候选文件计算哈希，按 (size, hash) 二级分组
    hash_groups = {}
    done = 0
    for size, files in size_groups.items():
        if len(files) < 2:
            continue
        for filepath in files:
            if stop_flag is not None and stop_flag.is_set():
                stopped = True
                break
            try:
                file_hash = compute_sha256(filepath)
            except OSError as e:
                if log_cb:
                    log_cb(f"[警告] 计算哈希失败，跳过：{filepath}（{e}）")
                continue
            hash_groups.setdefault((size, file_hash), []).append(filepath)
            done += 1
            if progress_cb:
                progress_cb(done, total, os.path.basename(filepath))
        if stopped:
            break

    if not candidates and progress_cb:
        progress_cb(1, 1, '')

    if stopped:
        if log_cb:
            log_cb("用户已停止，查重任务中断（已完成的哈希计算结果仍会用于本次查重）。")

    # 第三步：处理每个重复组，保留修改时间最早者，其余移动
    group_index = 0
    for (size, file_hash), files in hash_groups.items():
        if len(files) < 2:
            continue
        group_index += 1

        files_sorted = sorted(files, key=get_mtime_for_sort)
        keep = files_sorted[0]
        dups = files_sorted[1:]

        if log_cb:
            log_cb(
                f"[重复组 {group_index}] 大小：{human_size(size)}  哈希：{file_hash}\n"
                f"      保留文件：{keep}（修改时间：{get_mtime_str(keep)}）"
            )

        for dup_path in dups:
            dup_mtime_str = get_mtime_str(dup_path)  # 移动前先记录，避免移动后读取失败
            try:
                target = move_to_duplicate_dir(dup_path, dup_dir)
                dup_count += 1
                dup_relations.append({
                    'duplicate': dup_path,
                    'kept': keep,
                    'size': size,
                    'sha256': file_hash,
                    'moved_to': target,
                })
                if log_cb:
                    log_cb(
                        f"      重复文件：{dup_path}（修改时间：{dup_mtime_str}）\n"
                        f"      -> 移入：{target}"
                    )
            except Exception as e:
                errors.append(f"移动重复文件失败：{dup_path} : {e}")
                if log_cb:
                    log_cb(f"      [失败] 移动出错：{dup_path} : {e}")

    return dup_count, dup_relations, stopped, errors


# ============================================================
# 日志写入（文件名格式：videodedup_log_YYYYMMDD_NNN.txt）
# ============================================================

def write_log_file(library_dir, stats, extensions_text):
    """将本次运行的统计信息与重复文件对应关系写入日志文件"""
    lines = []
    lines.append("=" * 50)
    lines.append("VideoDedup 运行日志")
    lines.append(f"运行时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"处理扩展名：{extensions_text}")
    lines.append("判重方式：文件大小 + SHA-256 哈希完全一致")
    lines.append("=" * 50)
    lines.append("")

    lines.append(f"扫描视频文件总数：{stats['total_videos']}")
    lines.append(f"重复文件总数：{stats['total_dup_count']}")
    lines.append("")

    if stats['dup_relations']:
        lines.append("-" * 50)
        lines.append("重复文件对应关系列表：")
        for i, relation in enumerate(stats['dup_relations'], 1):
            lines.append(f"  {i}. 大小：{human_size(relation['size'])}")
            lines.append(f"     SHA-256：{relation['sha256']}")
            lines.append(f"     重复文件：{relation['duplicate']}")
            lines.append(f"     保留文件：{relation['kept']}")
            lines.append(f"     移动到：{relation['moved_to']}")
    else:
        lines.append("重复文件对应关系列表：（无）")
    lines.append("")

    if stats['errors']:
        lines.append("-" * 50)
        lines.append("错误明细：")
        for e in stats['errors']:
            lines.append(f"  {e}")
        lines.append("")

    elapsed_seconds = int(round(stats.get('elapsed_seconds', 0)))
    elapsed_minutes, elapsed_remainder = divmod(elapsed_seconds, 60)
    lines.append("-" * 50)
    lines.append(f"程序运行时长：{elapsed_minutes}分{elapsed_remainder}秒")
    if stats.get('stopped'):
        lines.append("（本次运行被用户手动停止，结果可能不完整）")

    date_str = datetime.now().strftime('%Y%m%d')
    log_content = '\n'.join(lines)
    sequence = 1
    while True:
        log_path = os.path.join(
            library_dir, f"videodedup_log_{date_str}_{sequence:03d}.txt"
        )
        try:
            # 独占创建，避免多个实例同时取得相同序号并覆盖日志。
            with open(log_path, 'x', encoding='utf-8') as f:
                f.write(log_content)
            return log_path
        except FileExistsError:
            sequence += 1


# ============================================================
# 核心逻辑：整理主流程
# ============================================================

def run_dedup(library_dir, extensions, progress_cb=None, log_cb=None, stop_flag=None):
    """
    对 library_dir 下所有视频文件执行整体查重，重复文件移入
    library_dir/重复视频文件/，并生成日志。

    返回 stats 字典。
    """
    start_time = time.perf_counter()
    stats = {
        'total_videos': 0,
        'total_dup_count': 0,
        'dup_relations': [],
        'errors': [],
        'stopped': False,
    }

    if log_cb:
        log_cb(f"开始扫描目录：{library_dir}")

    try:
        video_files = collect_video_files(library_dir, extensions)
    except Exception as e:
        stats['errors'].append(f"扫描目录失败：{e}")
        if log_cb:
            log_cb(f"[失败] 扫描目录出错：{e}")
        stats['elapsed_seconds'] = time.perf_counter() - start_time
        write_log_file(library_dir, stats, ','.join(sorted(extensions)))
        return stats

    stats['total_videos'] = len(video_files)
    if log_cb:
        log_cb(f"共找到视频文件 {len(video_files)} 个")

    dup_dir = os.path.join(library_dir, DUP_DIR_NAME)

    try:
        dup_count, dup_relations, stopped, dedup_errors = video_dedup(
            video_files, dup_dir, progress_cb=progress_cb, log_cb=log_cb, stop_flag=stop_flag
        )
        stats['total_dup_count'] = dup_count
        stats['dup_relations'] = dup_relations
        stats['stopped'] = stopped
        stats['errors'].extend(dedup_errors)
    except Exception as e:
        stats['errors'].append(f"查重过程出错：{e}")
        if log_cb:
            log_cb(f"[失败] 查重过程出错：{e}")

    if log_cb:
        log_cb(f"\n===== 完成：共 {stats['total_dup_count']} 个重复文件已移入「{DUP_DIR_NAME}」 =====")

    stats['elapsed_seconds'] = time.perf_counter() - start_time
    write_log_file(library_dir, stats, ','.join(sorted(extensions)))
    return stats


# ============================================================
# 图形界面
# ============================================================

class VideoDedupApp:
    def __init__(self, root):
        self.root = root
        root.title("VideoDedup 视频查重整理工具")
        root.geometry("780x600")
        root.minsize(700, 520)

        self.msg_queue = queue.Queue()
        self.stop_flag = threading.Event()
        self.worker_thread = None

        self._build_ui()
        self._load_last_settings()
        self.root.after(100, self._poll_queue)

    # ---------------------------------------------------
    def _build_ui(self):
        FONT = ('Microsoft YaHei UI', 10)
        FONT_BOLD = ('Microsoft YaHei UI', 16, 'bold')
        FONT_SMALL = ('Microsoft YaHei UI', 9)

        # 标题
        tk.Label(self.root, text="VideoDedup 视频查重整理工具", font=FONT_BOLD).pack(pady=(16, 4))
        tk.Label(
            self.root,
            text="按 大小+SHA-256哈希 判断重复视频，重复文件移入「重复视频文件」目录",
            font=FONT_SMALL, fg='#666666'
        ).pack(pady=(0, 12))

        # 1. 视频库目录
        frame1 = tk.Frame(self.root)
        frame1.pack(fill='x', padx=16, pady=4)
        tk.Label(frame1, text="1. 视频库目录：", width=14, anchor='w', font=FONT).pack(side='left')
        self.library_var = tk.StringVar()
        tk.Entry(frame1, textvariable=self.library_var, font=FONT).pack(
            side='left', fill='x', expand=True, padx=(0, 8))
        tk.Button(frame1, text="浏览", width=8, command=self._choose_library).pack(side='left')

        # 2. 视频扩展名
        frame2 = tk.Frame(self.root)
        frame2.pack(fill='x', padx=16, pady=8)
        tk.Label(frame2, text="2. 视频扩展名：", width=14, anchor='w', font=FONT).pack(side='left')
        self.extensions_var = tk.StringVar(value=DEFAULT_EXTENSIONS_TEXT)
        tk.Entry(frame2, textvariable=self.extensions_var, font=FONT).pack(
            side='left', fill='x', expand=True, padx=(0, 8))

        # 说明文字
        info_frame = tk.Frame(self.root)
        info_frame.pack(fill='x', padx=16, pady=(0, 8))
        tk.Label(
            info_frame,
            text="扩展名用英文逗号分隔，如 .mp4,.mov,.avi,.mkv,.m4v；判重仅依据 大小完全一致 + SHA-256哈希完全一致",
            font=FONT_SMALL, fg='#888888', justify='left'
        ).pack(side='left')

        # 进度条
        frame5 = tk.Frame(self.root)
        frame5.pack(fill='x', padx=16, pady=(8, 4))
        self.progress = ttk.Progressbar(frame5, orient='horizontal', mode='determinate')
        self.progress.pack(fill='x')

        # 状态行
        self.status_var = tk.StringVar(value="就绪 - 请选择视频库目录")
        tk.Label(self.root, textvariable=self.status_var, fg='#1a6fc4', font=FONT_SMALL).pack(pady=(2, 8))

        # 操作按钮
        btn_frame = tk.Frame(self.root)
        btn_frame.pack(fill='x', padx=16, pady=(0, 8))
        self.start_btn = tk.Button(
            btn_frame, text="开始查重整理", font=('Microsoft YaHei UI', 13, 'bold'),
            bg='#4caf50', fg='white', activebackground='#43a047',
            height=2, command=self._start
        )
        self.start_btn.pack(side='left', fill='x', expand=True, padx=(0, 6))

        self.stop_btn = tk.Button(
            btn_frame, text="停止", font=('Microsoft YaHei UI', 13),
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
    def _choose_library(self):
        d = filedialog.askdirectory(title="选择视频库目录")
        if d:
            self.library_var.set(d)
            save_config({
                'last_library': d,
                'extensions': self.extensions_var.get(),
            })

    def _load_last_settings(self):
        """启动时读取上次的视频库目录与扩展名设置并预填"""
        cfg = load_config()
        last_library = cfg.get('last_library', '')
        if last_library:
            self.library_var.set(last_library)
        last_extensions = cfg.get('extensions')
        if last_extensions:
            self.extensions_var.set(last_extensions)

    # ---------------------------------------------------
    def _start(self):
        library = self.library_var.get().strip()
        extensions_text = self.extensions_var.get().strip()
        extensions = parse_extensions(extensions_text)

        if not library or not os.path.isdir(library):
            messagebox.showerror("错误", "请选择有效的视频库目录")
            return

        if not extensions:
            messagebox.showerror("错误", "请至少指定一个有效的视频扩展名")
            return

        if not messagebox.askyesno(
            "确认",
            f"将扫描目录：{library}\n"
            f"处理扩展名：{', '.join(sorted(extensions))}\n\n"
            "查出的重复文件将被移动到「重复视频文件」目录（源处不留）。\n"
            "此操作不可逆，是否继续？"
        ):
            return

        save_config({'last_library': library, 'extensions': extensions_text})

        self.log_text.delete('1.0', 'end')
        self.progress['value'] = 0
        self.start_btn.config(state='disabled', text="查重中...")
        self.stop_btn.config(state='normal')
        self.status_var.set("正在扫描并查重，请稍候...")
        self.stop_flag.clear()

        self.worker_thread = threading.Thread(
            target=self._run_worker, args=(library, extensions), daemon=True
        )
        self.worker_thread.start()

    def _stop(self):
        self.stop_flag.set()
        self.stop_btn.config(state='disabled')
        self.status_var.set("正在停止...")

    # ---------------------------------------------------
    def _run_worker(self, library, extensions):
        def progress_cb(done, total, name):
            self.msg_queue.put(('progress', done, total, name))

        def log_cb(text):
            self.msg_queue.put(('log', text))

        try:
            stats = run_dedup(
                library, extensions,
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
                    _, done, total, name = item
                    self.progress['maximum'] = total
                    self.progress['value'] = done
                    if name:
                        self.status_var.set(f"计算哈希中 ({done}/{total})：{name}")
                    else:
                        self.status_var.set("正在整理结果...")

                elif kind == 'log':
                    self.log_text.insert('end', item[1] + '\n')
                    self.log_text.see('end')

                elif kind == 'done':
                    stats = item[1]
                    self.start_btn.config(state='normal', text="开始查重整理")
                    self.stop_btn.config(state='disabled')
                    self.status_var.set(
                        f"完成 - 视频总数 {stats['total_videos']}，"
                        f"重复文件 {stats['total_dup_count']}"
                    )
                    messagebox.showinfo(
                        "查重完成",
                        f"视频文件总数：{stats['total_videos']}\n"
                        f"重复文件总数：{stats['total_dup_count']}\n\n"
                        f"详细日志已写入视频库目录下的 videodedup_log_YYYYMMDD_NNN.txt"
                    )

                elif kind == 'error':
                    self.start_btn.config(state='normal', text="开始查重整理")
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
    VideoDedupApp(root)
    root.mainloop()


if __name__ == '__main__':
    main()
