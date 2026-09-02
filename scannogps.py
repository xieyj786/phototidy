# -*- coding: utf-8 -*-
"""扫描已按 PhotoRename 规则命名、但 GPS 位置需要重试的照片。

扫描结果使用 ``photorename_log_*.txt`` 格式写入图片库根目录，因而可以
直接作为 photorename.py 的下一次 GPS 重试输入。
"""

from __future__ import annotations

import argparse
import re
import sys
from time import perf_counter
from datetime import datetime
from pathlib import Path
from typing import Callable


IMAGE_EXTS = {".jpg", ".jpeg", ".heic", ".heif"}
LOG_PREFIX = "photorename_log"
TARGET_LOCATIONS = ("无GPS", "GPS位置未知")

# 地理位置字段按命名格式定义为不含下划线的一个字段；日期和序号必须严格为数字。
IMG_NAME_RE = re.compile(
    r"^IMG_(?P<date>\d{8})_(?P<location>[^_]+)_(?P<sequence>\d{4})$"
)
TIME_NAME_RES = {
    "DSC": re.compile(r"^DSC_\d{8}_\d{2}-\d{2}-\d{2}_\d{4}$"),
    "PIC": re.compile(r"^PIC_\d{8}_\d{2}-\d{2}-\d{2}_\d{4}$"),
}


def scan_files(root: Path) -> tuple[list[Path], dict[str, list[Path]], dict[str, int]]:
    """递归扫描 root，返回 IMG 文件、GPS 分组及 DSC/PIC 统计。"""
    matched: list[Path] = []
    by_location = {location: [] for location in TARGET_LOCATIONS}
    type_counts = {prefix: 0 for prefix in TIME_NAME_RES}

    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.casefold() not in IMAGE_EXTS:
            continue
        img_match = IMG_NAME_RE.fullmatch(path.stem)
        if img_match is None:
            for prefix, pattern in TIME_NAME_RES.items():
                if pattern.fullmatch(path.stem):
                    type_counts[prefix] += 1
                    break
            continue
        resolved = path.resolve()
        matched.append(resolved)
        location = img_match.group("location")
        if location in by_location:
            by_location[location].append(resolved)

    matched.sort(key=lambda item: str(item).casefold())
    for paths in by_location.values():
        paths.sort(key=lambda item: str(item).casefold())
    return matched, by_location, type_counts


def next_log_path(root: Path) -> Path:
    """返回不覆盖已有日志的递增日志路径。"""
    date_text = datetime.now().strftime("%Y%m%d")
    sequence = 1
    while True:
        candidate = root / f"{LOG_PREFIX}_{date_text}_{sequence:03d}.txt"
        if not candidate.exists():
            return candidate
        sequence += 1


def write_log(
    root: Path,
    matched: list[Path],
    by_location: dict[str, list[Path]],
    type_counts: dict[str, int],
    elapsed: float,
) -> Path:
    """写出与 photorename.py 兼容的日志。"""
    lines = [
        "=" * 60,
        "PhotoScanGPS 运行日志",
        f"运行时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
        f"图片库目录：{root}",
        "=" * 60,
        "",
        f"符合 IMG_YYYYMMDD_地理位置_NNNN 格式的 JPG/JPEG/HEIC/HEIF 文件：{len(matched)} 个",
        f"符合 DSC_YYYYMMDD_HH-MM-SS_NNNN 格式的文件：{type_counts['DSC']} 个",
        f"符合 PIC_YYYYMMDD_HH-MM-SS_NNNN 格式的文件：{type_counts['PIC']} 个",
        f"无GPS：{len(by_location['无GPS'])} 个",
        f"GPS位置未知：{len(by_location['GPS位置未知'])} 个",
        "",
        "无GPS 文件名对应：",
    ]
    lines.extend(f"  {path} -> {path}" for path in by_location["无GPS"])
    if not by_location["无GPS"]:
        lines.append("  （无）")
    lines.append("GPS位置未知 文件名对应：")
    lines.extend(f"  {path} -> {path}" for path in by_location["GPS位置未知"])
    if not by_location["GPS位置未知"]:
        lines.append("  （无）")
    lines.extend(["", f"程序运行时长：{elapsed:.2f} 秒"])

    log_path = next_log_path(root)
    # 独占创建，避免并行运行时覆盖另一份扫描结果。
    with log_path.open("x", encoding="utf-8") as file:
        file.write("\n".join(lines))
    return log_path


def scan(root: str | Path, log_cb: Callable[[str], None] | None = None) -> dict:
    """执行扫描并写日志，供命令行和图形界面共同使用。"""
    started = perf_counter()
    root_path = Path(root).expanduser().resolve()
    if not root_path.is_dir():
        raise NotADirectoryError(f"目录不存在或不可访问：{root_path}")
    matched, by_location, type_counts = scan_files(root_path)
    log_path = write_log(root_path, matched, by_location, type_counts, perf_counter() - started)
    if log_cb:
        log_cb(f"扫描完成：符合 IMG 命名格式的图片 {len(matched)} 个")
        log_cb(f"DSC：{type_counts['DSC']} 个；PIC：{type_counts['PIC']} 个")
        log_cb(f"无GPS：{len(by_location['无GPS'])} 个；GPS位置未知：{len(by_location['GPS位置未知'])} 个")
        log_cb(f"日志已写入：{log_path}")
    return {
        "root": root_path,
        "matched": matched,
        "by_location": by_location,
        "type_counts": type_counts,
        "log_path": log_path,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="递归统计并查找需要 GPS 重试的 PhotoRename 图片")
    parser.add_argument("directory", nargs="?", help="图片库根目录；省略时弹出目录选择窗口")
    args = parser.parse_args()

    directory = args.directory
    if not directory:
        try:
            import tkinter as tk
            from tkinter import filedialog, messagebox

            app = tk.Tk()
            app.withdraw()
            directory = filedialog.askdirectory(title="选择图片库根目录")
            if not directory:
                app.destroy()
                return 0
            result = scan(directory)
            messagebox.showinfo(
                "扫描完成",
                f"符合格式：{len(result['matched'])} 个\n"
                f"DSC：{result['type_counts']['DSC']} 个\n"
                f"PIC：{result['type_counts']['PIC']} 个\n"
                f"无GPS：{len(result['by_location']['无GPS'])} 个\n"
                f"GPS位置未知：{len(result['by_location']['GPS位置未知'])} 个\n\n"
                f"日志：{result['log_path']}",
            )
            app.destroy()
            return 0
        except ImportError:
            print("当前环境没有图形界面，请在命令行中提供图片库目录。", file=sys.stderr)
            return 2
    try:
        result = scan(directory, print)
    except (OSError, ValueError) as error:
        print(error, file=sys.stderr)
        return 1
    print(f"符合格式文件总数：{len(result['matched'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
