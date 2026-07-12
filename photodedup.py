#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PhotoDedup —— 照片查重整理工具

功能概述：
    对经过 PhotoTidy 整理的图片库目录进行查重清理。查重以「一级子目录」为单位
    独立进行，不同一级子目录之间互不查重：

        Photo_Library/
        ├── YYYY年/                    # 一级子目录（按年份+月份+视频/其他图片分类）
        ├── 一级子目录/                 # 用户自定义的一级子目录（如旅游照片等）
        ├── 其他一级子目录/
        ├── 重复图片文件/
        │   ├── YYYY年/                # YYYY年/ 下查出的重复文件（平铺存放）
        │   ├── 一级子目录/            # 一级子目录/ 下查出的重复文件（平铺存放）
        │   └── ...
        └── photodedup_log_YYYYMMDD_NNN.txt  # 运行日志

    每个一级子目录的查重分两步：

    步骤1 - 硬查重（拍摄时间相同的文件）：
        对 .jpg/.jpeg 文件之间、以及 .heic/.heif 文件之间（两者不互相比较），
        按 EXIF 拍摄时间精确到秒分组，组内执行两轮去重：
          第一轮：MD5 完全一致的文件保留体积最大者，其余移入「重复图片文件」对应子目录；
          第二轮：MD5 不同但视觉相似的文件（如同一张照片经不同压缩/传输产生的版本），
                  用 dHash 汉明距离判断相似性，相似的保留体积最大者，其余移入重复目录。

    步骤2 - 感知哈希查重（dHash + 直方图相关系数，仅 .jpg/.jpeg）：
        a) 无拍摄时间的文件之间互相比较，相似命中时保留体积最大的，其余视为重复；
        b) 有拍摄时间的文件与（步骤a后剩余的）无拍摄时间文件比较，
           相似命中时只保留有拍摄时间的文件，无拍摄时间的视为重复；
        c) 有拍摄时间的文件之间不进行感知哈希比较。

    相似度判定的 dHash 汉明距离阈值可在界面上调节。
"""

import os
import re
import shutil
import threading
import queue
import hashlib
import time
import warnings
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from collections import defaultdict
from typing import Iterable, Optional, cast

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageOps

# 可选：HEIC / HEIF 支持（若安装了 pillow-heif，则可读取其 EXIF 拍摄时间）
try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass


# ============================================================
# 常量定义
# ============================================================

JPEG_EXTS = {'.jpg', '.jpeg'}
HEIC_EXTS = {'.heic', '.heif'}
HARD_DEDUP_EXTS = JPEG_EXTS | HEIC_EXTS

# EXIF 中可能包含拍摄时间的标签：DateTimeOriginal, DateTimeDigitized, DateTime
EXIF_DATETIME_TAGS = (36867, 36868, 306)
EXIF_IFD_TAG = 0x8769  # Exif SubIFD

DUP_DIR_NAME = '重复图片文件'
LOG_PREFIX = 'photodedup_log'

# 配置文件：保存在用户主目录下，跨次运行记忆上次目录与阈值设置
CONFIG_PATH = os.path.join(os.path.expanduser('~'), '.photodedup_config.json')

DEFAULT_DHASH_THRESHOLD = 1  # dHash 汉明距离阈值默认值（经验值）
SAME_TIME_DHASH_THRESHOLD = 8
SAME_TIME_HIST_CONFIRM_THRESHOLD = 0.90


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
# 核心逻辑：EXIF 时间读取（与 PhotoTidy 一致）
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
    try:
        with warnings.catch_warnings():
            warnings.filterwarnings('ignore', category=UserWarning, module=r'PIL\..*')
            img = Image.open(filepath)
            with img:
                exif = img.getexif()
            if not exif:
                return None

            candidates = []
            try:
                exif_ifd = exif.get_ifd(EXIF_IFD_TAG)
                for tag in EXIF_DATETIME_TAGS:
                    if tag in exif_ifd:
                        candidates.append(exif_ifd[tag])
            except Exception:
                pass

            for tag in EXIF_DATETIME_TAGS:
                if tag in exif:
                    candidates.append(exif[tag])

            for value in candidates:
                dt = parse_exif_datetime(value)
                if dt is not None:
                    return dt
    except Exception:
        return None
    return None


def get_filename_datetime(filepath):
    """从常见照片文件名中解析拍摄时间，失败返回 None。"""
    name = os.path.basename(filepath)
    patterns = (
        r'(?<!\d)(\d{4})(\d{2})(\d{2})[_-](\d{2})(\d{2})(\d{2})(?!\d)',
        r'(?<!\d)(\d{4})[-_](\d{2})[-_](\d{2})[ _-](\d{2})[-_](\d{2})[-_](\d{2})(?!\d)',
    )
    for pattern in patterns:
        m = re.search(pattern, name)
        if not m:
            continue
        try:
            y, mo, d, h, mi, s = map(int, m.groups())
            return datetime(y, mo, d, h, mi, s)
        except ValueError:
            return None
    return None


def get_filename_datetime_key(filepath):
    """从文件名提取规范化时间戳 key，用于识别同一原始文件的副本命名。"""
    name = os.path.basename(filepath)
    patterns = (
        r'(?<!\d)(\d{4})(\d{2})(\d{2})[_-](\d{2})(\d{2})(\d{2})(?!\d)',
        r'(?<!\d)(\d{4})[-_](\d{2})[-_](\d{2})[ _-](\d{2})[-_](\d{2})[-_](\d{2})(?!\d)',
    )
    for pattern in patterns:
        m = re.search(pattern, name)
        if m:
            return ''.join(m.groups())
    return None


def get_capture_datetime(filepath):
    """优先读取 EXIF 拍摄时间，缺失时回退到文件名时间戳。"""
    return get_exif_datetime(filepath) or get_filename_datetime(filepath)


# ============================================================
# 核心逻辑：MD5 文件哈希（硬查重用）
# ============================================================

def compute_md5(filepath, chunk_size=1024 * 1024):
    """计算文件的 MD5 哈希值（十六进制字符串）"""
    h = hashlib.md5()
    with open(filepath, 'rb') as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


# ============================================================
# 核心逻辑：dHash 感知哈希（不依赖第三方 imagehash 库，纯 PIL 实现）
# ============================================================

def get_image_pixel_data(img) -> list[int]:
    """兼容新旧 Pillow 版本，返回图片像素数据。"""
    getter = getattr(img, 'get_flattened_data', None)
    if callable(getter):
        return list(cast(Iterable[int], getter()))
    return list(cast(Iterable[int], img.getdata()))


def compute_dhash(filepath, hash_size=8):
    """
    计算图片的 dHash（差异哈希）。
    算法：缩放到 (hash_size+1) x hash_size 的灰度图，比较每行相邻像素的大小关系，
    生成 hash_size * hash_size 位的二进制指纹，返回整数表示。
    失败返回 None。
    """
    try:
        with Image.open(filepath) as img:
            img = ImageOps.exif_transpose(img)
            img = img.convert('L').resize(
                (hash_size + 1, hash_size), Image.Resampling.LANCZOS
            )
            pixels = list(cast(Iterable[int], get_image_pixel_data(img)))

            bits = []
            for row in range(hash_size):
                row_start = row * (hash_size + 1)
                for col in range(hash_size):
                    left = pixels[row_start + col]
                    right = pixels[row_start + col + 1]
                    bits.append(1 if left > right else 0)

            value = 0
            for b in bits:
                value = (value << 1) | b
            return value
    except Exception:
        return None


def ensure_dhashes(infos):
    """Compute missing dHash values in parallel for a list of file info dicts."""
    missing = [info for info in infos if 'dhash' not in info]
    if not missing:
        return
    if len(missing) < 16:
        for info in missing:
            info['dhash'] = compute_dhash(info['path'])
        return

    max_workers = min(8, os.cpu_count() or 4, len(missing))
    paths = [info['path'] for info in missing]
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for info, dhash in zip(missing, executor.map(compute_dhash, paths)):
            info['dhash'] = dhash


def hamming_distance(a, b):
    """两个整数表示的哈希之间的汉明距离（不同比特位数）"""
    return bin(a ^ b).count('1')


def get_cached_file_size(info):
    """Return cached file size for an info dict, computing it once."""
    if '_size' not in info:
        info['_size'] = file_size_safe(info['path'])
    return info['_size']


def get_dhash_candidate_pairs_bktree(infos, threshold):
    """
    Generate exact candidate pairs whose dHash Hamming distance is <= threshold.
    This avoids full pairwise comparison for large same-time groups while preserving results.
    """
    if len(infos) < 2:
        return set()

    indexed_hashes = [
        (idx, info.get('dhash'))
        for idx, info in enumerate(infos)
        if info.get('dhash') is not None
    ]
    if len(indexed_hashes) < 2:
        return set()

    pairs = set()
    root = None

    def query(node, target_hash, target_idx):
        node_hash, node_idx, children = node
        dist = hamming_distance(target_hash, node_hash)
        if dist <= threshold:
            a, b = node_idx, target_idx
            pairs.add((a, b) if a < b else (b, a))
        low = max(0, dist - threshold)
        high = dist + threshold
        for child_dist, child in children.items():
            if low <= child_dist <= high:
                query(child, target_hash, target_idx)

    def insert(node, item_hash, item_idx):
        node_hash, _, children = node
        dist = hamming_distance(item_hash, node_hash)
        child = children.get(dist)
        if child is None:
            children[dist] = [item_hash, item_idx, {}]
        else:
            insert(child, item_hash, item_idx)

    for idx, dh in indexed_hashes:
        if root is None:
            root = [dh, idx, {}]
            continue
        query(root, dh, idx)
        insert(root, dh, idx)

    return pairs


def select_dhash_chunk_bits(infos, threshold) -> Optional[int]:
    """
    根据阈值和样本规模自适应选择 dHash 分块大小。
    阈值越小，块更大，筛选更高效；阈值越大，回退为更保守的全量比较。
    """
    if not infos or len(infos) < 32:
        return None
    if threshold <= 1:
        return 16
    if threshold <= 3:
        return 8
    if threshold <= 5:
        return 4
    return None


def build_dhash_bucket_index(infos, chunk_bits: Optional[int] = 8):
    """
    按 dHash 分块构建索引，返回 bucket_map。
    仅在 threshold <= chunk_bits 时可明显减少候选比较量。
    """
    if chunk_bits is None:
        return defaultdict(list)

    bucket_map = defaultdict(list)
    mask = (1 << chunk_bits) - 1
    chunks = 64 // chunk_bits
    for idx, info in enumerate(infos):
        dh = info.get('dhash')
        if dh is None:
            continue
        for chunk_idx in range(chunks):
            key = (chunk_idx, (dh >> (chunk_idx * chunk_bits)) & mask)
            bucket_map[key].append(idx)
    return bucket_map


def get_dhash_candidate_pairs(infos, threshold, chunk_bits: Optional[int] = None):
    """
    基于 dHash 分块索引生成候选比较对，避免全对比。
    若没有合适的块大小，则退回全量比较，保证正确性。
    """
    n = len(infos)
    if n < 2:
        return set()
    if threshold > 5 and n >= 32:
        return get_dhash_candidate_pairs_bktree(infos, threshold)

    chosen_bits = chunk_bits if chunk_bits is not None else select_dhash_chunk_bits(infos, threshold)
    if chosen_bits is None:
        return {(i, j) for i in range(n) for j in range(i + 1, n)}

    bucket_map = build_dhash_bucket_index(infos, chunk_bits=chosen_bits)
    pairs = set()
    for bucket in bucket_map.values():
        if len(bucket) < 2:
            continue
        for i in range(len(bucket)):
            for j in range(i + 1, len(bucket)):
                a, b = bucket[i], bucket[j]
                pairs.add((a, b) if a < b else (b, a))
    return pairs


def get_dhash_candidate_indices(info, bucket_map, chunk_bits: Optional[int] = 8):
    """
    从 bucket_map 中读取与单个 dHash 信息共享分块的候选索引。
    """
    dh = info.get('dhash')
    if dh is None or chunk_bits is None:
        return set()
    mask = (1 << chunk_bits) - 1
    chunks = 64 // chunk_bits
    result = set()
    for chunk_idx in range(chunks):
        key = (chunk_idx, (dh >> (chunk_idx * chunk_bits)) & mask)
        result.update(bucket_map.get(key, []))
    return result


def compute_hist_corr(filepath, size=(256, 256), bins=64):
    """
    计算图片灰度直方图，返回其归一化后的直方图数组。
    对 JPEG 重压缩、HDR/色调映射等处理具有很强的鲁棒性：
    相同内容不同压缩的图片，直方图相关系数通常 ≥ 0.999；
    不同内容的图片，相关系数通常接近 0 甚至为负数。
    失败返回 None。
    """
    try:
        with Image.open(filepath) as img:
            img = ImageOps.exif_transpose(img)
            gray = img.convert('L').resize(size, Image.Resampling.LANCZOS)
            raw_hist = gray.histogram()
        if bins == 256:
            return raw_hist
        values_per_bin = 256 // bins
        if values_per_bin > 0 and 256 % bins == 0:
            return [
                sum(raw_hist[i * values_per_bin:(i + 1) * values_per_bin])
                for i in range(bins)
            ]
        hist = [0] * bins
        for value, count in enumerate(raw_hist):
            idx = min(int(value * bins / 256), bins - 1)
            hist[idx] += count
        return hist
    except Exception:
        return None


def hist_corr_score(hist_a, hist_b):
    """
    计算两个直方图的 Pearson 相关系数，范围 [-1, 1]。
    值越接近 1 表示越相似。
    """
    if hist_a is None or hist_b is None:
        return -1.0
    n = len(hist_a)
    mean_a = sum(hist_a) / n
    mean_b = sum(hist_b) / n
    num = sum((hist_a[i] - mean_a) * (hist_b[i] - mean_b) for i in range(n))
    den_a = sum((hist_a[i] - mean_a) ** 2 for i in range(n))
    den_b = sum((hist_b[i] - mean_b) ** 2 for i in range(n))
    denom = (den_a * den_b) ** 0.5
    if denom == 0:
        return 1.0 if num == 0 else -1.0
    return num / denom


# 直方图相关系数只作为 dHash 命中后的辅助确认，避免单靠色调分布误判。
HIST_CORR_CONFIRM_THRESHOLD = 0.98


def is_visually_similar(info_a, info_b, dhash_threshold):
    """
    判断两张图片是否视觉相似。
    必须先满足 dHash 汉明距离阈值；直方图只用于辅助确认，不再单独触发判重。
    """
    h_a = info_a.get('dhash')
    h_b = info_b.get('dhash')
    if h_a is None or h_b is None:
        return False, None
    if hamming_distance(h_a, h_b) > dhash_threshold:
        return False, None

    if 'hist' not in info_a:
        info_a['hist'] = compute_hist_corr(info_a['path'])
    if 'hist' not in info_b:
        info_b['hist'] = compute_hist_corr(info_b['path'])

    hist_a = info_a.get('hist')
    hist_b = info_b.get('hist')
    if hist_a is None or hist_b is None:
        return True, 'dHash'
    if hist_corr_score(hist_a, hist_b) >= HIST_CORR_CONFIRM_THRESHOLD:
        return True, 'dHash+直方图'
    return False, None


def is_same_time_visually_similar(info_a, info_b, dhash_threshold):
    """同一拍摄时间组内的视觉判定，允许更宽松的 dHash 候选但仍需直方图确认。"""
    similar, reason = is_visually_similar(info_a, info_b, dhash_threshold)
    if similar:
        return similar, reason

    h_a = info_a.get('dhash')
    h_b = info_b.get('dhash')
    if h_a is None or h_b is None:
        return False, None
    if hamming_distance(h_a, h_b) > SAME_TIME_DHASH_THRESHOLD:
        return False, None

    if 'hist' not in info_a:
        info_a['hist'] = compute_hist_corr(info_a['path'])
    if 'hist' not in info_b:
        info_b['hist'] = compute_hist_corr(info_b['path'])

    if hist_corr_score(info_a.get('hist'), info_b.get('hist')) >= SAME_TIME_HIST_CONFIRM_THRESHOLD:
        return True, '同秒dHash+直方图'
    return False, None


# ============================================================
# 核心逻辑：文件操作辅助
# ============================================================

def get_unique_target_path(target_dir, filename):
    """若目标路径已存在同名文件，自动在文件名后追加 _1, _2 ... 避免覆盖"""
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


def move_to_duplicate_dir(filepath, dup_dir):
    """将重复文件移动到指定的重复图片目录（平铺存放），返回目标路径"""
    os.makedirs(dup_dir, exist_ok=True)
    target_path = get_unique_target_path(dup_dir, os.path.basename(filepath))
    shutil.move(filepath, target_path)
    return target_path


def file_size_safe(filepath):
    """安全获取文件体积，失败返回 -1"""
    try:
        return os.path.getsize(filepath)
    except OSError:
        return -1


# ============================================================
# 核心逻辑：扫描一级子目录下的所有图片文件
# ============================================================

def scan_image_files(top_level_dir):
    """
    递归扫描一级子目录下所有 .jpg/.jpeg/.heic/.heif 文件。
    返回 file_info 列表，每项为字典：
        {
            'path': 完整路径,
            'ext': 小写后缀,
            'exif_dt': EXIF 拍摄时间（datetime 或 None）,
        }
    """
    results = []
    for dirpath, dirnames, filenames in os.walk(top_level_dir):
        # 不扫描"重复图片文件"目录本身（防止误把已查出的重复文件再次扫描）
        if os.path.basename(dirpath) == DUP_DIR_NAME:
            dirnames[:] = []
            continue
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext not in HARD_DEDUP_EXTS:
                continue
            filepath = os.path.join(dirpath, fn)
            real_exif_dt = get_exif_datetime(filepath)
            filename_dt = get_filename_datetime(filepath)
            exif_dt = real_exif_dt or filename_dt
            filename_dt_key = get_filename_datetime_key(filepath)
            results.append({
                'path': filepath,
                'ext': ext,
                'exif_dt': exif_dt,
                'real_exif_dt': real_exif_dt,
                'filename_dt_key': filename_dt_key,
            })
    return results


def global_md5_dedup(file_infos, dup_dir, log_cb=None):
    """
    在同一个一级子目录内按 MD5 全局查重，不依赖 EXIF 拍摄时间。
    完全相同的文件保留体积最大者，其余移入 dup_dir。
    """
    dup_count = 0
    dup_names = []
    dup_relations = []
    md5_groups = defaultdict(list)

    for info in file_infos:
        if info.get('removed'):
            continue
        try:
            md5_val = compute_md5(info['path'])
            info['_md5'] = md5_val
        except Exception as e:
            info['_md5'] = None
            if log_cb:
                log_cb(f"[全局MD5-警告] 无法计算 MD5：{info['path']} : {e}")
            continue
        md5_groups[md5_val].append(info)

    for md5_val, same_infos in md5_groups.items():
        if len(same_infos) < 2:
            continue
        same_infos.sort(key=get_cached_file_size, reverse=True)
        keep = same_infos[0]
        for dup_info in same_infos[1:]:
            try:
                target = move_to_duplicate_dir(dup_info['path'], dup_dir)
                dup_info['removed'] = True
                dup_count += 1
                dup_names.append(os.path.basename(target))
                dup_relations.append({
                    'method': '全局 MD5 完全重复',
                    'duplicate': dup_info['path'],
                    'kept': keep['path'],
                    'moved_to': target,
                })
                if log_cb:
                    log_cb(
                        f"[全局MD5重复] {dup_info['path']}\n"
                        f"      与保留文件内容完全一致：{keep['path']}\n"
                        f"      -> 移入：{target}"
                    )
            except Exception as e:
                if log_cb:
                    log_cb(f"[全局MD5-失败] {dup_info['path']} : {e}")

    return dup_count, dup_names, dup_relations


# ============================================================
# 核心逻辑：步骤1 —— 硬查重（文件完全一致）
# ============================================================

def hard_dedup(file_infos, dup_dir, threshold, log_cb=None):
    """
    硬查重：按 EXIF 拍摄时间（精确到秒）分组，.jpg/.jpeg 与 .heic/.heif 分开分组。
    对同组内 MD5 不同但视觉相似的 jpg/jpeg 文件，用 dHash 判断相似性；
    相似的保留体积最大者，其余移入 dup_dir。

    参数：
        file_infos: scan_image_files() 返回的列表（会原地标记 'removed': True 的项）
        threshold : dHash 汉明距离阈值（与感知哈希查重步骤共用同一阈值）
    返回：
        (dup_count, dup_names, dup_relations)  本步骤产生的重复文件数、文件名列表与对应关系
    """
    dup_count = 0
    dup_names = []
    dup_relations = []

    # 仅处理有 EXIF 拍摄时间的文件；按 (拍摄时间精确到秒, 扩展名组) 分组
    groups = defaultdict(list)
    for info in file_infos:
        if info.get('removed'):
            continue
        if info['exif_dt'] is None:
            continue
        ext_group = 'jpeg' if info['ext'] in JPEG_EXTS else 'heic'
        key = (info['exif_dt'].strftime('%Y-%m-%d %H:%M:%S'), ext_group)
        groups[key].append(info)

    hard_visual_candidates = []
    for infos in groups.values():
        if len(infos) < 2:
            continue
        hard_visual_candidates.extend(
            info for info in infos
            if not info.get('removed') and info['ext'] in JPEG_EXTS and info.get('_md5')
        )
    ensure_dhashes(hard_visual_candidates)

    for key, infos in groups.items():
        if len(infos) < 2:
            continue

        # ------ 视觉相似（仅 jpg/jpeg，拍摄时间相同但 MD5 不同）------
        survivors = [
            info for info in infos
            if not info.get('removed') and info['ext'] in JPEG_EXTS and info.get('_md5')
        ]
        if len(survivors) < 2:
            continue

        # 文件名时间戳相同且同处一个拍摄时间组，通常是 "(1)"、"_1" 这类副本命名。
        filename_groups = defaultdict(list)
        for info in survivors:
            key_name = info.get('filename_dt_key')
            if key_name:
                filename_groups[key_name].append(info)

        for filename_key, same_name_infos in filename_groups.items():
            active_infos = [info for info in same_name_infos if not info.get('removed')]
            if len(active_infos) < 2:
                continue
            active_infos.sort(key=get_cached_file_size, reverse=True)
            keep = active_infos[0]
            for dup_info in active_infos[1:]:
                try:
                    target = move_to_duplicate_dir(dup_info['path'], dup_dir)
                    dup_info['removed'] = True
                    dup_count += 1
                    dup_names.append(os.path.basename(target))
                    dup_relations.append({
                        'method': '同秒拍摄文件名副本重复',
                        'duplicate': dup_info['path'],
                        'kept': keep['path'],
                        'moved_to': target,
                    })
                    if log_cb:
                        log_cb(
                            f"[硬查重-文件名副本重复] {dup_info['path']}\n"
                            f"      与保留文件拍摄时间/文件名时间戳相同：{keep['path']}\n"
                            f"      -> 移入：{target}"
                        )
                except Exception as e:
                    if log_cb:
                        log_cb(f"[硬查重-失败] {dup_info['path']} : {e}")

        survivors = [info for info in survivors if not info.get('removed')]
        if len(survivors) < 2:
            continue

        # 计算 dHash（直方图只在 dHash 命中后按需计算）
        ensure_dhashes(survivors)

        # 完整聚类：遍历候选对，构建相似关系并合并同一组
        parent = {}

        def find(idx):
            if idx not in parent:
                parent[idx] = idx
            if parent[idx] != idx:
                parent[idx] = find(parent[idx])
            return parent[idx]

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        same_time_threshold = max(threshold, SAME_TIME_DHASH_THRESHOLD)
        candidate_pairs = get_dhash_candidate_pairs(survivors, same_time_threshold)
        for i, j in candidate_pairs:
            info_i = survivors[i]
            info_j = survivors[j]
            similar, _ = is_same_time_visually_similar(info_i, info_j, threshold)
            if similar:
                union(i, j)

        clusters = defaultdict(list)
        for idx, info in enumerate(survivors):
            clusters[find(idx)].append(info)

        for group in clusters.values():
            if len(group) < 2:
                continue
            group.sort(key=get_cached_file_size, reverse=True)
            keep = group[0]
            for dup_info in group[1:]:
                if dup_info.get('removed'):
                    continue
                try:
                    target = move_to_duplicate_dir(dup_info['path'], dup_dir)
                    dup_info['removed'] = True
                    dup_count += 1
                    dup_names.append(os.path.basename(target))
                    dup_relations.append({
                        'method': '同秒拍摄视觉重复',
                        'duplicate': dup_info['path'],
                        'kept': keep['path'],
                        'moved_to': target,
                    })
                    if log_cb:
                        log_cb(
                            f"[硬查重-视觉重复] {dup_info['path']}\n"
                            f"      拍摄时间相同且视觉相似，保留体积较大者：{keep['path']}\n"
                            f"      -> 移入：{target}"
                        )
                except Exception as e:
                    if log_cb:
                        log_cb(f"[硬查重-失败] {dup_info['path']} : {e}")

    return dup_count, dup_names, dup_relations


# ============================================================
# 核心逻辑：步骤2 —— 感知哈希查重（dHash，仅 jpg/jpeg）
# ============================================================

def phash_dedup(file_infos, dup_dir, threshold, log_cb=None):
    """
    感知哈希查重，仅针对 .jpg/.jpeg 且尚未在硬查重中被标记移除的文件：
        a) 无拍摄时间文件之间互相比较，相似的保留体积最大者；
        b) 有拍摄时间文件与（a 之后剩余的）无拍摄时间文件比较，相似时只保留有拍摄时间的；
        c) 有拍摄时间文件之间不比较。

    返回：(dup_count, dup_names, dup_relations)
    """
    dup_count = 0
    dup_names = []
    dup_relations = []

    candidates = [
        info for info in file_infos
        if not info.get('removed') and info['ext'] in JPEG_EXTS
    ]
    with_time = [
        info for info in candidates
        if info.get('real_exif_dt') is not None
    ]
    without_time = [
        info for info in candidates
        if info.get('real_exif_dt') is None
    ]
    candidates = without_time

    # 先只计算 dHash；直方图在 dHash 命中后按需计算，减少大图库耗时。
    ensure_dhashes(candidates)

    without_time = [info for info in without_time if info['dhash'] is not None]

    # ---------- a) 无拍摄时间文件之间互相比较 ----------
    # 使用完整聚类，处理传递相似性的情况
    parent = {}

    def find(idx):
        if idx not in parent:
            parent[idx] = idx
        if parent[idx] != idx:
            parent[idx] = find(parent[idx])
        return parent[idx]

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    candidate_pairs = get_dhash_candidate_pairs(without_time, threshold)
    for i, j in candidate_pairs:
        info_i = without_time[i]
        info_j = without_time[j]
        similar, _ = is_visually_similar(info_i, info_j, threshold)
        if similar:
            union(i, j)

    clusters = defaultdict(list)
    for idx, info in enumerate(without_time):
        clusters[find(idx)].append(info)

    for group in clusters.values():
        if len(group) < 2:
            continue

        group.sort(key=get_cached_file_size, reverse=True)
        keep = group[0]
        for dup_info in group[1:]:
            if dup_info.get('removed'):
                continue
            try:
                target = move_to_duplicate_dir(dup_info['path'], dup_dir)
                dup_info['removed'] = True
                dup_count += 1
                dup_names.append(os.path.basename(target))
                dup_relations.append({
                    'method': '感知哈希重复（无时间组）',
                    'duplicate': dup_info['path'],
                    'kept': keep['path'],
                    'moved_to': target,
                })
                if log_cb:
                    log_cb(
                        f"[感知哈希-重复(无时间组)] {dup_info['path']}\n"
                        f"      与保留文件视觉相似，保留体积最大者：{keep['path']}\n"
                        f"      -> 移入：{target}"
                    )
            except Exception as e:
                if log_cb:
                    log_cb(f"[感知哈希-失败] {dup_info['path']} : {e}")

    # ---------- b) 有拍摄时间文件 与 剩余无拍摄时间文件 比较 ----------
    remaining_without_time = [info for info in without_time if not info.get('removed')]
    if not remaining_without_time:
        return dup_count, dup_names, dup_relations

    ensure_dhashes(with_time)
    with_time = [info for info in with_time if info['dhash'] is not None]

    chunk_bits = select_dhash_chunk_bits(remaining_without_time, threshold)
    bucket_map = build_dhash_bucket_index(remaining_without_time, chunk_bits=chunk_bits) if chunk_bits is not None else None
    for info_t in with_time:
        candidate_idxs = get_dhash_candidate_indices(info_t, bucket_map, chunk_bits=chunk_bits) if bucket_map is not None else set(range(len(remaining_without_time)))
        for idx in candidate_idxs:
            info_n = remaining_without_time[idx]
            if info_n.get('removed'):
                continue
            similar, _ = is_visually_similar(info_t, info_n, threshold)
            if similar:
                try:
                    target = move_to_duplicate_dir(info_n['path'], dup_dir)
                    info_n['removed'] = True
                    dup_count += 1
                    dup_names.append(os.path.basename(target))
                    dup_relations.append({
                        'method': '感知哈希重复（与有时间文件比较）',
                        'duplicate': info_n['path'],
                        'kept': info_t['path'],
                        'moved_to': target,
                    })
                    if log_cb:
                        log_cb(
                            f"[感知哈希-重复(与有时间文件比较)] {info_n['path']}\n"
                            f"      与有拍摄时间文件相似，优先保留：{info_t['path']}\n"
                            f"      -> 移入：{target}"
                        )
                except Exception as e:
                    if log_cb:
                        log_cb(f"[感知哈希-失败] {info_n['path']} : {e}")

    return dup_count, dup_names, dup_relations


# ============================================================
# 核心逻辑：识别一级子目录
# ============================================================

def list_top_level_dirs(library_dir):
    """
    列出 library_dir 下所有一级子目录，排除「重复图片文件」目录本身。
    返回完整路径列表。
    """
    result = []
    try:
        for name in sorted(os.listdir(library_dir)):
            full = os.path.join(library_dir, name)
            if not os.path.isdir(full):
                continue
            if name == DUP_DIR_NAME:
                continue
            result.append(full)
    except FileNotFoundError:
        pass
    return result


# ============================================================
# 核心逻辑：日志写入
# ============================================================

def write_log_file(library_dir, stats, threshold):
    """
    将本次运行的统计信息写入带日期和递增序号的日志文件，
    包含各一级子目录下的重复文件数和重复文件对应关系。
    """
    lines = []
    lines.append("=" * 50)
    lines.append("PhotoDedup 运行日志")
    lines.append(f"运行时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"感知哈希(dHash)相似度阈值：{threshold}")
    lines.append("=" * 50)
    lines.append("")

    lines.append(f"扫描的一级子目录总数：{len(stats['top_level_results'])}")
    lines.append(f"图片文件总数（.jpg/.jpeg/.heic/.heif）：{stats['total_images']}")
    lines.append(f"重复文件总数：{stats['total_dup_count']}")
    lines.append("")

    for top_name, info in stats['top_level_results'].items():
        lines.append("-" * 50)
        lines.append(f"一级子目录：{top_name}")
        lines.append(f"  图片文件数：{info['image_count']}")
        lines.append(f"  全局 MD5 完全重复文件数：{info['global_md5_dup_count']}")
        lines.append(f"  同秒拍摄视觉重复文件数：{info['hard_dup_count']}")
        lines.append(f"  感知哈希查重重复文件数：{info['phash_dup_count']}")
        lines.append(
            f"  重复文件总数：{info['global_md5_dup_count'] + info['hard_dup_count'] + info['phash_dup_count']}"
        )
        all_dup_relations = (
            info.get('global_md5_dup_relations', [])
            + info.get('hard_dup_relations', [])
            + info.get('phash_dup_relations', [])
        )
        if all_dup_relations:
            lines.append("  重复文件对应关系列表：")
            for i, relation in enumerate(all_dup_relations, 1):
                lines.append(f"    {i}. 查重方式：{relation['method']}")
                lines.append(f"       重复文件：{relation['duplicate']}")
                lines.append(f"       保留文件：{relation['kept']}")
                lines.append(f"       移动到：{relation['moved_to']}")
        else:
            lines.append("  重复文件对应关系列表：（无）")
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

    date_text = datetime.now().strftime('%Y%m%d')
    log_content = '\n'.join(lines)
    sequence = 1
    while True:
        filename = f"{LOG_PREFIX}_{date_text}_{sequence:03d}.txt"
        log_path = os.path.join(library_dir, filename)
        try:
            with open(log_path, 'x', encoding='utf-8') as f:
                f.write(log_content)
            return log_path
        except FileExistsError:
            sequence += 1


# ============================================================
# 核心逻辑：整理主流程
# ============================================================

def run_dedup(library_dir, threshold, progress_cb=None, log_cb=None, stop_flag=None):
    """
    对 library_dir 下每个一级子目录独立执行查重，结果写入
    library_dir/重复图片文件/<一级子目录名>/，并生成日志。

    返回 stats 字典。
    """
    start_time = time.perf_counter()
    stats = {
        'total_images': 0,
        'total_dup_count': 0,
        'top_level_results': {},
        'errors': [],
    }

    top_dirs = list_top_level_dirs(library_dir)
    dup_root = os.path.join(library_dir, DUP_DIR_NAME)

    total_dirs = len(top_dirs) or 1

    for idx, top_dir in enumerate(top_dirs):
        if stop_flag is not None and stop_flag.is_set():
            if log_cb:
                log_cb("用户已停止，查重任务中断。")
            break

        top_name = os.path.basename(top_dir)
        if log_cb:
            log_cb(f"===== 开始处理一级子目录：{top_name} =====")

        try:
            file_infos = scan_image_files(top_dir)
        except Exception as e:
            stats['errors'].append(f"{top_dir} : 扫描失败 - {e}")
            if log_cb:
                log_cb(f"[失败] 扫描目录出错：{top_dir} : {e}")
            if progress_cb:
                progress_cb(idx + 1, total_dirs, top_name)
            continue

        image_count = len(file_infos)
        stats['total_images'] += image_count

        this_dup_dir = os.path.join(dup_root, top_name)

        try:
            global_count, global_names, global_relations = global_md5_dedup(
                file_infos, this_dup_dir, log_cb=log_cb
            )
        except Exception as e:
            global_count, global_names, global_relations = 0, [], []
            stats['errors'].append(f"{top_dir} : 全局 MD5 查重出错 - {e}")
            if log_cb:
                log_cb(f"[失败] 全局 MD5 查重出错：{top_dir} : {e}")

        try:
            hard_count, hard_names, hard_relations = hard_dedup(file_infos, this_dup_dir, threshold, log_cb=log_cb)
        except Exception as e:
            hard_count, hard_names, hard_relations = 0, [], []
            stats['errors'].append(f"{top_dir} : 硬查重出错 - {e}")
            if log_cb:
                log_cb(f"[失败] 硬查重出错：{top_dir} : {e}")

        try:
            phash_count, phash_names, phash_relations = phash_dedup(
                file_infos, this_dup_dir, threshold, log_cb=log_cb
            )
        except Exception as e:
            phash_count, phash_names, phash_relations = 0, [], []
            stats['errors'].append(f"{top_dir} : 感知哈希查重出错 - {e}")
            if log_cb:
                log_cb(f"[失败] 感知哈希查重出错：{top_dir} : {e}")

        stats['top_level_results'][top_name] = {
            'image_count': image_count,
            'global_md5_dup_count': global_count,
            'global_md5_dup_names': global_names,
            'global_md5_dup_relations': global_relations,
            'hard_dup_count': hard_count,
            'hard_dup_names': hard_names,
            'hard_dup_relations': hard_relations,
            'phash_dup_count': phash_count,
            'phash_dup_names': phash_names,
            'phash_dup_relations': phash_relations,
        }
        stats['total_dup_count'] += global_count + hard_count + phash_count

        if log_cb:
            log_cb(
                f"===== 完成：{top_name}（全局 MD5 {global_count} 个，"
                f"同秒视觉查重 {hard_count} 个，"
                f"感知哈希查重 {phash_count} 个）=====\n"
            )

        if progress_cb:
            progress_cb(idx + 1, total_dirs, top_name)

    stats['elapsed_seconds'] = time.perf_counter() - start_time
    stats['log_path'] = write_log_file(library_dir, stats, threshold)
    return stats


# ============================================================
# 图形界面
# ============================================================

class PhotoDedupApp:
    def __init__(self, root):
        self.root = root
        root.title("PhotoDedup 照片查重整理工具")
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
        tk.Label(self.root, text="PhotoDedup 照片查重整理工具", font=FONT_BOLD).pack(pady=(16, 4))
        tk.Label(
            self.root,
            text="对各一级子目录独立查重，重复文件移入「重复图片文件」目录",
            font=FONT_SMALL, fg='#666666'
        ).pack(pady=(0, 12))

        # 1. 图片库目录
        frame1 = tk.Frame(self.root)
        frame1.pack(fill='x', padx=16, pady=4)
        tk.Label(frame1, text="1. 图片库目录：", width=14, anchor='w', font=FONT).pack(side='left')
        self.library_var = tk.StringVar()
        tk.Entry(frame1, textvariable=self.library_var, font=FONT).pack(
            side='left', fill='x', expand=True, padx=(0, 8))
        tk.Button(frame1, text="浏览", width=8, command=self._choose_library).pack(side='left')

        # 2. 感知哈希阈值
        frame2 = tk.Frame(self.root)
        frame2.pack(fill='x', padx=16, pady=8)
        tk.Label(frame2, text="2. 感知哈希相似度阈值：", width=20, anchor='w', font=FONT).pack(side='left')

        self.threshold_var = tk.IntVar(value=DEFAULT_DHASH_THRESHOLD)
        self.threshold_scale = tk.Scale(
            frame2, from_=0, to=3, orient='horizontal',
            variable=self.threshold_var, length=300,
            command=self._on_threshold_change
        )
        self.threshold_scale.pack(side='left', padx=(0, 8))

        self.threshold_label = tk.Label(frame2, text=str(DEFAULT_DHASH_THRESHOLD), font=FONT, width=4)
        self.threshold_label.pack(side='left')

        tk.Label(
            frame2, text="（dHash 汉明距离，数值越小要求越相似，越大判重越宽松）",
            font=FONT_SMALL, fg='#999999'
        ).pack(side='left', padx=(8, 0))

        # 说明文字
        info_frame = tk.Frame(self.root)
        info_frame.pack(fill='x', padx=16, pady=(0, 8))
        tk.Label(
            info_frame,
            text="查重流程：1) MD5 硬查重（同秒拍摄）+ 视觉相似查重（dHash OR 直方图）  2) 无时间文件感知查重（仅 jpg/jpeg）",
            font=FONT_SMALL, fg='#888888', justify='left'
        ).pack(side='left')

        # 进度条
        frame5 = tk.Frame(self.root)
        frame5.pack(fill='x', padx=16, pady=(8, 4))
        self.progress = ttk.Progressbar(frame5, orient='horizontal', mode='determinate')
        self.progress.pack(fill='x')

        # 状态行
        self.status_var = tk.StringVar(value="就绪 - 请选择图片库目录")
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
    def _on_threshold_change(self, value):
        self.threshold_label.config(text=str(int(float(value))))

    def _choose_library(self):
        d = filedialog.askdirectory(title="选择图片库目录")
        if d:
            self.library_var.set(d)
            save_config({
                'last_library': d,
                'threshold': self.threshold_var.get(),
            })

    def _load_last_settings(self):
        """启动时读取上次的图片库目录与阈值设置并预填"""
        cfg = load_config()
        last_library = cfg.get('last_library', '')
        if last_library:
            self.library_var.set(last_library)
        last_threshold = cfg.get('threshold')
        if isinstance(last_threshold, int):
            self.threshold_var.set(last_threshold)
            self.threshold_label.config(text=str(last_threshold))

    # ---------------------------------------------------
    def _start(self):
        library = self.library_var.get().strip()
        threshold = self.threshold_var.get()

        if not library or not os.path.isdir(library):
            messagebox.showerror("错误", "请选择有效的图片库目录")
            return

        top_dirs = list_top_level_dirs(library)
        if not top_dirs:
            messagebox.showwarning("提示", "该目录下没有可供查重的一级子目录")
            return

        if not messagebox.askyesno(
            "确认",
            f"将对图片库下 {len(top_dirs)} 个一级子目录分别查重，\n"
            f"感知哈希相似度阈值：{threshold}\n\n"
            "查出的重复文件将被移动到「重复图片文件」目录（源处不留）。\n"
            "此操作不可逆，是否继续？"
        ):
            return

        save_config({'last_library': library, 'threshold': threshold})

        self.log_text.delete('1.0', 'end')
        self.progress['value'] = 0
        self.start_btn.config(state='disabled', text="查重中...")
        self.stop_btn.config(state='normal')
        self.status_var.set("正在扫描并查重，请稍候...")
        self.stop_flag.clear()

        self.worker_thread = threading.Thread(
            target=self._run_worker, args=(library, threshold), daemon=True
        )
        self.worker_thread.start()

    def _stop(self):
        self.stop_flag.set()
        self.stop_btn.config(state='disabled')
        self.status_var.set("正在停止...")

    # ---------------------------------------------------
    def _run_worker(self, library, threshold):
        def progress_cb(done, total, name):
            self.msg_queue.put(('progress', done, total, name))

        def log_cb(text):
            self.msg_queue.put(('log', text))

        try:
            stats = run_dedup(
                library, threshold,
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
                    self.status_var.set(f"处理中 ({done}/{total})：{name}")

                elif kind == 'log':
                    self.log_text.insert('end', item[1] + '\n')
                    self.log_text.see('end')

                elif kind == 'done':
                    stats = item[1]
                    self.start_btn.config(state='normal', text="开始查重整理")
                    self.stop_btn.config(state='disabled')
                    self.status_var.set(
                        f"完成 - 图片总数 {stats['total_images']}，"
                        f"重复文件 {stats['total_dup_count']}"
                    )
                    messagebox.showinfo(
                        "查重完成",
                        f"扫描一级子目录数：{len(stats['top_level_results'])}\n"
                        f"图片文件总数：{stats['total_images']}\n"
                        f"重复文件总数：{stats['total_dup_count']}\n\n"
                        f"详细日志已写入图片库目录下的 "
                        f"{os.path.basename(stats.get('log_path', ''))}"
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
    PhotoDedupApp(root)
    root.mainloop()


if __name__ == '__main__':
    main()
