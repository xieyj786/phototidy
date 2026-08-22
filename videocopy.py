#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VideoCopy —— 视频文件拷贝工具

功能：
    递归扫描源目录（/photodata）下所有子目录，将视频文件连同其子目录结构
    完整拷贝到目标目录（/videodata）下的对应子目录中。

规则：
    - 支持的视频后缀：.mov / .avi / .mp4 / .m2ts
    - 同目录下的非视频文件不拷贝
    - 完全不含视频文件的子目录，在目标端不创建对应目录
    - 目标端已存在同名文件时，自动在文件名后追加 _1、_2 … 避免覆盖
    - 目标目录按需创建（增量拷贝，可多次运行）
    - 每次运行在目标根目录下写入日志文件（videocopy_log_YYYYMMDD_NNN.txt，递增命名）

配置：
    上次使用的源目录和目标目录保存在 ~/.videocopy_config.json，
    下次启动时自动预填。
"""

import os
import shutil
import threading
import queue
import json
from datetime import datetime

import tkinter as tk
from tkinter import filedialog, messagebox, ttk


# ============================================================
# 常量定义
# ============================================================

VIDEO_EXTS = {'.mov', '.avi', '.mp4', '.m2ts'}
LOG_PREFIX = 'videocopy_log'
CONFIG_PATH = os.path.join(os.path.expanduser('~'), '.videocopy_config.json')


def normalize_exts(text):
    """将逗号分隔的后缀规范为小写的 .ext 集合。"""
    result = set()
    for item in text.split(','):
        item = item.strip().lower()
        if item:
            result.add(item if item.startswith('.') else '.' + item)
    return result


# ============================================================
# 配置读写
# ============================================================

def load_config():
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(data):
    try:
        with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ============================================================
# 文件操作辅助
# ============================================================

def get_unique_target_path(target_dir, filename):
    """若目标路径已存在同名文件，自动追加 _1, _2 … 后缀避免覆盖"""
    target_path = os.path.join(target_dir, filename)
    if not os.path.exists(target_path):
        return target_path
    name, ext = os.path.splitext(filename)
    i = 1
    while True:
        candidate = os.path.join(target_dir, f"{name}_{i}{ext}")
        if not os.path.exists(candidate):
            return candidate
        i += 1


# ============================================================
# 日志文件命名（日期 + 递增序号）
# ============================================================

def _next_log_path(target_dir):
    os.makedirs(target_dir, exist_ok=True)
    date_text = datetime.now().strftime('%Y%m%d')
    sequence = 1
    while True:
        filename = f"{LOG_PREFIX}_{date_text}_{sequence:03d}.txt"
        candidate = os.path.join(target_dir, filename)
        if not os.path.exists(candidate):
            return candidate
        sequence += 1


def write_log(target_dir, stats):
    log_path = _next_log_path(target_dir)
    stats['log_path'] = log_path
    start_time = stats['start_time']
    end_time = datetime.now()
    elapsed = end_time - start_time

    lines = []
    lines.append('=' * 50)
    lines.append('VideoCopy 运行日志')
    lines.append('=' * 50)
    lines.append('')
    lines.append(f"源目录：  {stats['source_dir']}")
    lines.append(f"目标目录：{stats['target_dir']}")
    lines.append(f"开始时间：{start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"结束时间：{end_time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"耗时：    {int(elapsed.total_seconds())} 秒")
    lines.append('')
    lines.append('-' * 50)
    lines.append('视频文件统计')
    lines.append('-' * 50)
    lines.append(f"扫描文件总数：{stats['total_files']}")
    lines.append(f"视频文件总数：{stats['video_files']}")
    lines.append('')
    lines.append('按后缀汇总：')
    for ext, count in sorted(stats['ext_counts'].items()):
        lines.append(f"  {ext:<8} {count} 个")
    lines.append('')
    lines.append('按子目录统计（源目录下各一级子目录的视频文件数量）：')
    for sub, ext_map in sorted(stats['ext_counts_in_dir'].items()):
        sub_total = sum(ext_map.values())
        ext_detail = '  '.join(f"{e} {c}个" for e, c in sorted(ext_map.items()))
        lines.append(f"  {sub}：共 {sub_total} 个  （{ext_detail}）")
    lines.append('')
    lines.append('-' * 50)
    lines.append('拷贝结果')
    lines.append('-' * 50)
    lines.append(f"成功拷贝：     {stats['success_count']} 个")
    lines.append(f"重命名拷贝：   {stats['rename_count']} 个（目标已有同名文件，自动追加序号）")
    lines.append(f"失败：         {stats['fail_count']} 个")
    lines.append('')
    if stats['fail_details']:
        lines.append('失败明细：')
        for d in stats['fail_details']:
            lines.append(f"  {d}")
        lines.append('')
    with open(log_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    return log_path


# ============================================================
# 核心逻辑：扫描 + 拷贝
# ============================================================

def copy_videos(source_dir, target_dir, extra_exts=None,
                progress_cb=None, log_cb=None, stop_flag=None):
    """
    递归扫描 source_dir，将所有视频文件拷贝到 target_dir 下的对应子目录。

    返回 stats 字典。
    """
    video_exts = set(VIDEO_EXTS) | set(extra_exts or set())
    stats = {
        'source_dir': source_dir,
        'target_dir': target_dir,
        'start_time': datetime.now(),      # 程序开始运行时间
        'total_files': 0,
        'video_files': 0,
        'ext_counts': {},                  # 各后缀视频文件数量
        'ext_counts_in_dir': {},           # 各子目录下各后缀视频文件数量
        'success_count': 0,
        'fail_count': 0,
        'rename_count': 0,
        'fail_details': [],
    }

    # 先完整扫描，收集所有视频文件（便于显示总进度）
    video_list = []   # (src_path, rel_path)
    total_scanned = 0

    for dirpath, dirnames, filenames in os.walk(source_dir):
        # 跳过目标目录本身（防止源目录与目标目录有重叠时死循环）
        dirnames[:] = [
            d for d in dirnames
            if os.path.abspath(os.path.join(dirpath, d)) != os.path.abspath(target_dir)
        ]
        for fn in filenames:
            total_scanned += 1
            ext = os.path.splitext(fn)[1].lower()
            if ext in video_exts:
                src_path = os.path.join(dirpath, fn)
                rel_path = os.path.relpath(src_path, source_dir)
                video_list.append((src_path, rel_path))
                # 统计各子目录（一级子目录名）的后缀分布
                parts = rel_path.split(os.sep)
                sub = parts[0] if len(parts) > 1 else '（根目录）'
                if sub not in stats['ext_counts_in_dir']:
                    stats['ext_counts_in_dir'][sub] = {}
                stats['ext_counts_in_dir'][sub][ext] =                     stats['ext_counts_in_dir'][sub].get(ext, 0) + 1

    stats['total_files'] = total_scanned
    stats['video_files'] = len(video_list)
    total = len(video_list) or 1

    for idx, (src_path, rel_path) in enumerate(video_list):
        if stop_flag is not None and stop_flag.is_set():
            if log_cb:
                log_cb('用户已停止，拷贝任务中断。')
            break

        ext = os.path.splitext(rel_path)[1].lower()
        stats['ext_counts'][ext] = stats['ext_counts'].get(ext, 0) + 1

        # 目标子目录 = target_dir / 相对路径的目录部分
        rel_dir = os.path.dirname(rel_path)
        dst_dir = os.path.join(target_dir, rel_dir) if rel_dir else target_dir

        try:
            os.makedirs(dst_dir, exist_ok=True)
            filename = os.path.basename(src_path)
            dst_path = get_unique_target_path(dst_dir, filename)

            renamed = dst_path != os.path.join(dst_dir, filename)
            shutil.copy2(src_path, dst_path)

            stats['success_count'] += 1
            if renamed:
                stats['rename_count'] += 1
                if log_cb:
                    log_cb(f"[重命名拷贝] {src_path}\n      -> {dst_path}")
            else:
                if log_cb:
                    log_cb(f"[拷贝] {src_path}\n      -> {dst_path}")

        except Exception as e:
            stats['fail_count'] += 1
            stats['fail_details'].append(f"{src_path} : {e}")
            if log_cb:
                log_cb(f"[失败] {src_path} : {e}")

        if progress_cb:
            progress_cb(idx + 1, total, os.path.basename(src_path))

    write_log(target_dir, stats)
    return stats


# ============================================================
# 图形界面
# ============================================================

class VideoCopyApp:
    def __init__(self, root):
        self.root = root
        root.title('VideoCopy 视频文件拷贝工具')
        root.geometry('780x560')
        root.minsize(700, 480)

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

        tk.Label(self.root, text='VideoCopy 视频文件拷贝工具', font=FONT_BOLD).pack(pady=(16, 4))
        tk.Label(
            self.root,
            text='递归扫描源目录，将视频文件连同子目录结构拷贝到目标目录',
            font=FONT_SMALL, fg='#666666'
        ).pack(pady=(0, 12))

        # 1. 源目录
        f1 = tk.Frame(self.root)
        f1.pack(fill='x', padx=16, pady=4)
        tk.Label(f1, text='1. 源目录：', width=14, anchor='w', font=FONT).pack(side='left')
        self.source_var = tk.StringVar()
        tk.Entry(f1, textvariable=self.source_var, font=FONT).pack(
            side='left', fill='x', expand=True, padx=(0, 8))
        tk.Button(f1, text='浏览', width=8, command=self._choose_source).pack(side='left')

        # 2. 目标目录
        f2 = tk.Frame(self.root)
        f2.pack(fill='x', padx=16, pady=4)
        tk.Label(f2, text='2. 目标目录：', width=14, anchor='w', font=FONT).pack(side='left')
        self.target_var = tk.StringVar()
        tk.Entry(f2, textvariable=self.target_var, font=FONT).pack(
            side='left', fill='x', expand=True, padx=(0, 8))
        tk.Button(f2, text='浏览', width=8, command=self._choose_target).pack(side='left')

        # 3. 视频额外后缀
        f3 = tk.Frame(self.root)
        f3.pack(fill='x', padx=16, pady=4)
        tk.Label(f3, text='3. 视频额外后缀：', width=14, anchor='w', font=FONT).pack(side='left')
        self.extra_ext_var = tk.StringVar()
        tk.Entry(f3, textvariable=self.extra_ext_var, font=FONT).pack(
            side='left', fill='x', expand=True, padx=(0, 8))
        tk.Label(f3, text='默认 .mov,.mp4,.avi,.m2ts；可添加如 .m4v,.mts',
                 font=FONT_SMALL, fg='#999999').pack(side='left')

        # 支持的后缀说明
        f4 = tk.Frame(self.root)
        f4.pack(fill='x', padx=16, pady=(6, 0))
        tk.Label(
            f4,
            text='支持的视频后缀：.mov  .avi  .mp4  .m2ts    '
                 '（同目录下的其他文件不拷贝；目标已有同名文件时自动重命名）',
            font=FONT_SMALL, fg='#888888', justify='left'
        ).pack(side='left')

        # 进度条
        f4 = tk.Frame(self.root)
        f4.pack(fill='x', padx=16, pady=(12, 4))
        self.progress = ttk.Progressbar(f4, orient='horizontal', mode='determinate')
        self.progress.pack(fill='x')

        # 状态行
        self.status_var = tk.StringVar(value='就绪 - 请选择源目录和目标目录')
        tk.Label(self.root, textvariable=self.status_var,
                 fg='#1a6fc4', font=FONT_SMALL).pack(pady=(2, 8))

        # 操作按钮
        btn_frame = tk.Frame(self.root)
        btn_frame.pack(fill='x', padx=16, pady=(0, 8))
        self.start_btn = tk.Button(
            btn_frame, text='开始拷贝视频文件',
            font=('Microsoft YaHei UI', 13, 'bold'),
            bg='#4caf50', fg='white', activebackground='#43a047',
            height=2, command=self._start
        )
        self.start_btn.pack(side='left', fill='x', expand=True, padx=(0, 6))
        self.stop_btn = tk.Button(
            btn_frame, text='停止',
            font=('Microsoft YaHei UI', 13),
            bg='#e0e0e0', height=2, width=8,
            state='disabled', command=self._stop
        )
        self.stop_btn.pack(side='left')

        # 日志区
        log_frame = tk.Frame(self.root)
        log_frame.pack(fill='both', expand=True, padx=16, pady=(0, 16))
        self.log_text = tk.Text(log_frame, font=('Consolas', 9), wrap='none')
        scroll_y = tk.Scrollbar(log_frame, orient='vertical', command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll_y.set)
        self.log_text.pack(side='left', fill='both', expand=True)
        scroll_y.pack(side='right', fill='y')

    # ---------------------------------------------------
    def _choose_source(self):
        d = filedialog.askdirectory(title='选择源目录')
        if d:
            self.source_var.set(d)
            self._save_settings()

    def _choose_target(self):
        d = filedialog.askdirectory(title='选择目标目录')
        if d:
            self.target_var.set(d)
            self._save_settings()

    def _save_settings(self):
        save_config({
            'last_source': self.source_var.get(),
            'last_target': self.target_var.get(),
            'extra_exts': self.extra_ext_var.get(),
        })

    def _load_last_settings(self):
        cfg = load_config()
        if cfg.get('last_source'):
            self.source_var.set(cfg['last_source'])
        if cfg.get('last_target'):
            self.target_var.set(cfg['last_target'])
        self.extra_ext_var.set(cfg.get('extra_exts', ''))

    # ---------------------------------------------------
    def _start(self):
        source = self.source_var.get().strip()
        target = self.target_var.get().strip()

        if not source or not os.path.isdir(source):
            messagebox.showerror('错误', '请选择有效的源目录')
            return
        if not target:
            messagebox.showerror('错误', '请选择目标目录')
            return
        if os.path.abspath(source) == os.path.abspath(target):
            messagebox.showerror('错误', '源目录和目标目录不能相同')
            return

        try:
            os.makedirs(target, exist_ok=True)
        except Exception as e:
            messagebox.showerror('错误', f'无法创建目标目录：{e}')
            return

        self._save_settings()
        extra_exts = normalize_exts(self.extra_ext_var.get())
        self.log_text.delete('1.0', 'end')
        self.progress['value'] = 0
        self.start_btn.config(state='disabled', text='拷贝中...')
        self.stop_btn.config(state='normal')
        self.status_var.set('正在扫描并拷贝，请稍候...')
        self.stop_flag.clear()

        self.worker_thread = threading.Thread(
            target=self._run_worker, args=(source, target, extra_exts), daemon=True
        )
        self.worker_thread.start()

    def _stop(self):
        self.stop_flag.set()
        self.stop_btn.config(state='disabled')
        self.status_var.set('正在停止...')

    # ---------------------------------------------------
    def _run_worker(self, source, target, extra_exts):
        def progress_cb(done, total, filename):
            self.msg_queue.put(('progress', done, total, filename))

        def log_cb(text):
            self.msg_queue.put(('log', text))

        try:
            stats = copy_videos(
                source, target,
                extra_exts=extra_exts,
                progress_cb=progress_cb,
                log_cb=log_cb,
                stop_flag=self.stop_flag,
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
                    _, done, total, filename = item
                    self.progress['maximum'] = total
                    self.progress['value'] = done
                    self.status_var.set(
                        f'拷贝中 ({done}/{total})：{filename}')

                elif kind == 'log':
                    self.log_text.insert('end', item[1] + '\n')
                    self.log_text.see('end')

                elif kind == 'done':
                    stats = item[1]
                    self.start_btn.config(state='normal', text='开始拷贝视频文件')
                    self.stop_btn.config(state='disabled')
                    self.status_var.set(
                        f"完成 - 共拷贝 {stats['success_count']} 个视频文件"
                        f"（其中重命名 {stats['rename_count']} 个）"
                        f"，失败 {stats['fail_count']} 个"
                    )
                    messagebox.showinfo(
                        '拷贝完成',
                        f"扫描文件总数：{stats['total_files']}\n"
                        f"发现视频文件：{stats['video_files']}\n"
                        f"成功拷贝：{stats['success_count']}\n"
                        f"重命名拷贝（目标已有同名）：{stats['rename_count']}\n"
                        f"失败：{stats['fail_count']}\n\n"
                        f"日志已写入：{stats.get('log_path', '目标目录下的日志文件')}"
                    )

                elif kind == 'error':
                    self.start_btn.config(state='normal', text='开始拷贝视频文件')
                    self.stop_btn.config(state='disabled')
                    self.status_var.set('发生错误')
                    messagebox.showerror('错误', item[1])

        except queue.Empty:
            pass

        self.root.after(100, self._poll_queue)


# ============================================================
# 程序入口
# ============================================================

def main():
    root = tk.Tk()
    VideoCopyApp(root)
    root.mainloop()


if __name__ == '__main__':
    main()
