#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PhotoRename：按 EXIF 信息批量重新命名照片。"""

import os
import queue
import re
import threading
import time
import json
import urllib.parse
import urllib.request
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Literal, cast, overload
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, UnidentifiedImageError

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass


# PhotoRename only handles camera/photo formats. Screenshot and other image
# formats such as PNG must remain untouched.
IMAGE_EXTS = {'.jpg', '.jpeg', '.heic', '.heif'}
EXIF_REQUIRED_EXTS = IMAGE_EXTS
PHONE_MARKERS = ('apple', 'iphone', 'samsung', 'galaxy', 'pixel', 'huawei', 'honor',
                 'xiaomi', 'redmi', 'oppo', 'vivo', 'oneplus', 'realme', 'meizu',
                 'motorola', 'zte', 'nubia', '小米', '华为', '荣耀', '三星', '苹果')
LOG_PREFIX = 'photorename_log'


def clean(value):
    if isinstance(value, bytes):
        try:
            value = value.decode('ascii')
        except UnicodeDecodeError:
            value = value.decode('utf-8', errors='replace')
    return str(value).replace('\x00', '').strip() if value is not None else ''


def parse_datetime(value):
    match = re.match(r'^(\d{4})[:\-](\d{2})[:\-](\d{2})[ T](\d{2}):(\d{2}):(\d{2})', clean(value))
    if not match:
        return None
    try:
        year, month, day, hour, minute, second = map(int, match.groups())
        return datetime(year, month, day, hour, minute, second)
    except ValueError:
        return None


@overload
def read_exif(path: Path | str, include_gps: Literal[True]) -> tuple[str, str, datetime | None, tuple[float, float] | None]:
    ...


@overload
def read_exif(path: Path | str, include_gps: bool = False) -> tuple[str, str, datetime | None]:
    ...


def read_exif(path: Path | str, include_gps: bool = False) -> tuple[str, str, datetime | None] | tuple[str, str, datetime | None, tuple[float, float] | None]:
    with Image.open(path) as image:
        exif = image.getexif()
        make, model = clean(exif.get(271)), clean(exif.get(272))
        taken = None
        # 36867 = DateTimeOriginal, 36868 = DateTimeDigitized.
        # Do not normally use 306 (Image DateTime): it is often the file's
        # metadata modification time, not necessarily the moment the photo
        # was taken.  Some phone exports, however, preserve GPS and only
        # retain 306.  In that case the GPS is strong evidence that this is
        # a camera photo, so use 306 as a narrowly-scoped fallback below.
        exif_ifd = {}
        try:
            exif_ifd = exif.get_ifd(0x8769) or {}
        except (AttributeError, KeyError, TypeError, ValueError):
            pass
        for tag in (36867, 36868):
            taken = parse_datetime(exif.get(tag)) or parse_datetime(exif_ifd.get(tag))
            if taken:
                break
        gps = extract_gps(exif)
        if taken is None and gps:
            taken = parse_datetime(exif.get(306))
        result = (make, model, taken, gps)
        return result if include_gps else result[:3]


def is_phone(make, model):
    text = f'{make} {model}'.casefold()
    return any(marker.casefold() in text for marker in PHONE_MARKERS)


def has_camera_identity(make, model):
    """Return whether EXIF identifies the camera that took the photo.

    Both EXIF Make and Model must be present before a dated photo can be
    classified as a phone photo or an independent camera photo.  A photo
    without this identity may still be eligible for the PIC naming rule.
    """
    return bool(clean(make) and clean(model))


def _gps_value(value: object) -> float | None:
    try:
        # Pillow may expose a rational as IFDRational, a (numerator,
        # denominator) pair, or an ordinary numeric value depending on the
        # image/container and Pillow version.
        if isinstance(value, (tuple, list)) and len(value) == 2:
            return float(cast(float, value[0])) / float(cast(float, value[1]))
        return float(cast(float, value))
    except (TypeError, ValueError, ZeroDivisionError, IndexError, OverflowError):
        return None


def extract_gps(exif):
    # GPSInfo is an EXIF sub-IFD.  get_ifd() is required by newer Pillow
    # versions; get(34853) is retained as a fallback for mocked/older data.
    try:
        gps = exif.get_ifd(0x8825) or {}
    except (AttributeError, KeyError, TypeError, ValueError):
        gps = exif.get(34853) or {}
    # Pillow normally returns a mapping for GPSInfo, but malformed or
    # vendor-specific EXIF may contain a scalar (for example an integer).
    # Such metadata has no usable GPS coordinates and must not abort a batch.
    # Some files expose an empty GPS sub-IFD through get_ifd() while retaining
    # the raw GPSInfo mapping at tag 34853.  Do not discard that valid data.
    if not gps:
        gps = exif.get(34853) or {}
    if not isinstance(gps, Mapping):
        return None

    # Numeric keys are Pillow's normal representation; the names make this
    # tolerant of EXIF readers/mocks that return decoded GPS tag names.
    def gps_tag(number, name):
        return gps.get(number, gps.get(name))

    lat_ref, lon_ref = clean(gps_tag(1, 'GPSLatitudeRef')).upper(), clean(gps_tag(3, 'GPSLongitudeRef')).upper()
    lat_raw, lon_raw = gps_tag(2, 'GPSLatitude'), gps_tag(4, 'GPSLongitude')
    if lat_ref not in {'N', 'S'} or lon_ref not in {'E', 'W'} or not lat_raw or not lon_raw:
        return None
    try:
        lat = sum(_gps_value(part) * (60 ** -index) for index, part in enumerate(lat_raw))
        lon = sum(_gps_value(part) * (60 ** -index) for index, part in enumerate(lon_raw))
    except (TypeError, ValueError, ZeroDivisionError, IndexError, OverflowError):
        return None
    if lat_ref == 'S': lat = -lat
    if lon_ref == 'W': lon = -lon
    return (lat, lon) if -90 <= lat <= 90 and -180 <= lon <= 180 else None


_CITY_CACHE = {}
LOCATION_ZH = {
    'guangzhou': '广州',
    'guangzhou city': '广州市',
    'shenzhen': '深圳',
    'shenzhen city': '深圳市',
    'city of cape town': '\u5f00\u666e\u6566',
    'cape town': '\u5f00\u666e\u6566',
    'moses kotane local municipality': '\u83ab\u585e\u65af\u79d1\u5854\u5185',
    'randburg': '\u5170\u5fb7\u5821',
    'dubai': '\u8fea\u62dc',
    'emirate of dubai': '\u8fea\u62dc\u9177\u957f\u56fd',
    'united arab emirates': '\u963f\u62c9\u4f2f\u8054\u5408\u914b\u957f\u56fd',
    'south africa': '\u5357\u975e',
    # Auckland is returned in English for many New Zealand GPS points because
    # the corresponding OSM administrative object has no ``name:zh`` tag.
    'auckland': '\u5965\u514b\u5170',
    'auckland region': '\u5965\u514b\u5170\u5730\u533a',
    'new zealand': '\u65b0\u897f\u5170',
    # Nominatim may return local Arabic names even when the request asks for
    # Chinese; normalize the landmarks seen in this photo library as well.
    '\u0627\u0644\u062f\u0651\u064e\u0627\u0646\u064e\u0629\u0652': '\u8fbe\u7eb3',
    '\u0627\u0644\u0643\u064e\u0627\u0633\u0650\u0631': '\u5361\u65af\u5c14',
    '\u0627\u0644\u0631\u0648\u0636\u0629': '\u7f57\u8fbe',
    # Al Hosn is an administrative district in Abu Dhabi.  Nominatim returns
    # this Arabic spelling for the two 2014-12-02 photos.
    '\u0627\u0644\u062d\u0650\u0635\u0650\u0646': '\u827e\u8d6b\u68ee',
}


def location_in_chinese(value):
    """Return a confirmed Chinese location name, or an empty string.

    A default-language location must not leak into a filename.  Returning an
    empty value lets the caller try the next, broader administrative level.
    """
    value = clean(value)
    if re.search(r'[\u3400-\u9fff]', value):
        return value
    folded = value.casefold()
    if folded in LOCATION_ZH:
        return LOCATION_ZH[folded]
    for source, target in LOCATION_ZH.items():
        if source in folded:
            return target
    return ''


def chinese_administrative_name(address, namedetails):
    """Return the Chinese name of the reverse-geocoded administrative area.

    ``accept-language`` is not sufficient on its own: Nominatim can still
    return the local/default name in ``address``.  Its ``namedetails`` field
    contains the Chinese OSM name when one is available, so it must take
    precedence over every default-language address field.
    """
    address = address if isinstance(address, Mapping) else {}
    namedetails = namedetails if isinstance(namedetails, Mapping) else {}
    # OSM/Nominatim has used several spellings for the Chinese locale key.
    # Check all of them, including case variants, before looking at the
    # ordinary address fields (which are commonly in the local language).
    localized = {clean(key).casefold(): value for key, value in namedetails.items()}
    localized_address = {clean(key).casefold(): value for key, value in address.items()}
    chinese_name = (localized.get('name:zh-hans') or localized.get('name:zh')
                    or localized.get('name:zh-cn')
                    # Some Nominatim installations expose localised address
                    # values using these keys instead of namedetails.
                    or localized_address.get('name:zh-hans') or localized_address.get('name:zh')
                    or localized_address.get('name:zh-cn'))
    if chinese_name:
        return clean(chinese_name)
    # Prefer a locality when it has a confirmed Chinese name.  Otherwise
    # progressively broaden the scope; for example, Devonport -> Auckland,
    # rather than retaining the untranslated suburb in the filename.
    levels = ('city', 'town', 'village', 'municipality', 'city_district',
              'district', 'locality', 'suburb', 'neighbourhood', 'county',
              'state_district', 'state', 'region', 'country', 'country_code')
    for level in levels:
        chinese_value = location_in_chinese(address.get(level))
        if chinese_value:
            return chinese_value
    # A few Nominatim responses contain only a localized display name while
    # omitting the corresponding administrative field.
    display_name = location_in_chinese(address.get('display_name'))
    if display_name:
        return display_name
    return ''


def city_from_gps(gps):
    if not gps: return None
    # A 0.01-degree cell is sufficient for a filename locality and avoids
    # making one network request for every photo in the same area.
    key = (round(gps[0], 2), round(gps[1], 2))
    if key in _CITY_CACHE: return _CITY_CACHE[key]
    city = None
    try:
        query = urllib.parse.urlencode({'lat': gps[0], 'lon': gps[1], 'format': 'jsonv2',
                                        'zoom': 10, 'accept-language': 'zh-CN',
                                        'namedetails': 1})
        request = urllib.request.Request(
            f'https://nominatim.openstreetmap.org/reverse?{query}',
            headers={'User-Agent': 'PhotoRename/1.0', 'Accept-Language': 'zh-CN,zh;q=0.9'},
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            result = json.load(response)
            address = result.get('address', {})
            namedetails = result.get('namedetails', {})
            if isinstance(address, Mapping) and result.get('display_name'):
                address = dict(address)
                address['display_name'] = result['display_name']
        city = chinese_administrative_name(address, namedetails)
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    _CITY_CACHE[key] = clean(city) or None
    return _CITY_CACHE[key]


def make_target(path, filename, ext, reserved):
    number = 1
    while True:
        # If the filename already ends with a four-digit sequence, advance
        # that sequence on collision instead of appending a suffix after it.
        # This keeps the sequence as the final semantic component.
        match = re.search(r'_(\d{4})$', filename)
        if match:
            sequence = int(match.group(1)) + number - 1
            candidate = f'{filename[:match.start()]}_{sequence:04d}'
        else:
            suffix = '' if number == 1 else f'_{number}'
            candidate = f'{filename}{suffix}'
        target = path.parent / f'{candidate}{ext}'
        if not target.exists() and target.name not in reserved:
            break
        number += 1
    
    reserved.add(target.name)
    return target


def files_from_latest_log(root):
    """Return files listed for GPS retry in the newest log.

    ``None`` means there is no previous PhotoRename log and the caller should
    perform a full scan.  An empty list means a log exists but has no files
    eligible for another GPS-based naming pass.
    """
    root_path = Path(root)
    log_files = list(root_path.glob(f'{LOG_PREFIX}_*.txt'))
    if not log_files:
        return None
    latest_log = max(log_files, key=lambda path: path.stat().st_mtime)
    sections = {'无GPS 文件名对应：', 'GPS位置未知 文件名对应：'}
    listed = []
    seen = set()
    active = False
    try:
        lines = latest_log.read_text(encoding='utf-8').splitlines()
    except (OSError, UnicodeError):
        return []
    root_resolved = root_path.resolve()
    for line in lines:
        stripped = line.strip()
        if stripped in sections:
            active = True
            continue
        if active and stripped.endswith('文件名对应：'):
            active = False
        if not active or ' -> ' not in line:
            continue
        _, new_name = line.split(' -> ', 1)
        candidate = Path(new_name.strip())
        try:
            candidate_resolved = candidate.resolve()
            candidate_resolved.relative_to(root_resolved)
        except (OSError, ValueError):
            continue
        if (candidate_resolved.is_file()
                and candidate_resolved.suffix.casefold() in IMAGE_EXTS
                and candidate_resolved not in seen):
            seen.add(candidate_resolved)
            listed.append(candidate_resolved)
    return listed


def write_log(root, stats):
    lines = [('=' * 60), 'PhotoRename 运行日志',
             f"运行时间：{datetime.now():%Y-%m-%d %H:%M:%S}", f'图片库目录：{root}', '=' * 60, '',
             f"扫描图片文件：{stats['scanned']}", f"符合 EXIF 条件：{stats['eligible']}",
             f"成功重新命名：{stats['renamed']}", f"跳过文件：{stats['skipped']}",
             f"处理失败：{len(stats['errors'])}", '']
    if stats.get('debug_log', False):
        lines.append('重新命名文件明细：')
        lines.extend(f"  {old} -> {new}" for old, new in stats['renames'])
    lines += ['', '各后缀文件统计：']
    for ext in ('.jpg', '.jpeg', '.heic', '.heif'):
        item = stats['extension_stats'][ext]
        lines.append(f"  {ext}: 总数 {item['total']}，成功重命名 {item['renamed']}，未重新命名 {item['not_renamed']}")
    lines += ['', 'GPS 状态统计：',
              f"  无GPS：{len(stats['no_gps_renames'])} 个",
              f"  GPS位置未知：{len(stats['gps_unknown_renames'])} 个",
              '']
    lines.append('无GPS 文件名对应：')
    lines.extend(f"  {old} -> {new}" for old, new in stats['no_gps_renames'])
    if not stats['no_gps_renames']:
        lines.append('  （无）')
    lines.append('GPS位置未知 文件名对应：')
    lines.extend(f"  {old} -> {new}" for old, new in stats['gps_unknown_renames'])
    if not stats['gps_unknown_renames']:
        lines.append('  （无）')
    if stats.get('debug_log', False) and not stats['renames']:
        lines.append('  （无）')
    if stats['errors']:
        lines += ['', '错误明细：']
        lines.extend(f'  {error}' for error in stats['errors'])
    lines += ['', f"程序运行时长：{stats['elapsed']:.2f} 秒"]
    sequence = 1
    while True:
        target = Path(root) / f'{LOG_PREFIX}_{datetime.now():%Y%m%d}_{sequence:03d}.txt'
        try:
            with target.open('x', encoding='utf-8') as file:
                file.write('\n'.join(lines))
            return str(target)
        except FileExistsError:
            sequence += 1


def rename_photos(root, progress_cb=None, log_cb=None, stop_flag=None, debug_log=False):
    started = time.perf_counter()
    stats = {'scanned': 0, 'eligible': 0, 'renamed': 0, 'skipped': 0,
             'errors': [], 'renames': [], 'no_gps_renames': [],
             'gps_unknown_renames': [], 'elapsed': 0, 'debug_log': debug_log}
    retry_files = files_from_latest_log(root)
    if retry_files is None:
        files = [p for p in Path(root).rglob('*') if p.is_file() and p.suffix.casefold() in IMAGE_EXTS]
    else:
        files = retry_files
        stats['retry_mode'] = True
    stats['extension_stats'] = {
        ext: {'total': sum(p.suffix.casefold() == ext for p in files), 'renamed': 0, 'not_renamed': 0}
        for ext in ('.jpg', '.jpeg', '.heic', '.heif')
    }
    if retry_files == []:
        stats['elapsed'] = time.perf_counter() - started
        stats['log_path'] = None
        stats['no_work'] = True
        return stats
    reserved = set()
    sequences = {}
    records = []
    # 先读取全部元数据，才能为无 GPS 照片查找同目录同日期的城市。
    for scan_index, path in enumerate(files, 1):
        if stop_flag and stop_flag.is_set():
            stats['skipped'] += len(files) - scan_index + 1
            if log_cb: log_cb('用户已停止任务。')
            break
        try:
            make, model, taken, gps = read_exif(path, include_gps=True)
            # A usable EXIF capture time is required.  Do not fall back to
            # the file modification time: that is not necessarily the time
            # the photo was taken and would incorrectly rename files without
            # EXIF capture metadata.
            if taken is None:
                continue
            records.append((path, make, model, taken, gps,
                            city_from_gps(gps) if gps else None))
        except (OSError, ValueError, UnidentifiedImageError) as error:
            stats['errors'].append(f'{path}：{error}')
        if progress_cb:
            progress_cb(scan_index, len(files) or 1, path.name)
    gps_cities = {}
    for path, make, model, taken, gps, city in records:
        if city:
            key = (path.parent, taken.date())
            gps_cities.setdefault(key, city)
    record_map = {path: (make, model, taken, gps, city) for path, make, model, taken, gps, city in records}
    total = len(files) or 1
    for index, path in enumerate(files, 1):
        if stop_flag and stop_flag.is_set():
            stats['skipped'] += total - index + 1
            if log_cb: log_cb('用户已停止任务。')
            break
        stats['scanned'] += 1
        try:
            record = record_map.get(path)
            if not record:
                stats['skipped'] += 1
            else:
                make, model, taken, gps, city = record
                stats['eligible'] += 1
                complete_camera_info = has_camera_identity(make, model)
                phone = is_phone(make, model)
                prefix = ('IMG' if phone else 'DSC') if complete_camera_info else 'PIC'
                ext = path.suffix.lower()
                year_key = (prefix, taken.year)
                sequences[year_key] = sequences.get(year_key, 0) + 1
                if gps:
                    city = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '', clean(city)) if city else ''
                    # GPS was successfully extracted even when reverse
                    # geocoding is unavailable.  Keep that distinction clear
                    # in the filename instead of falsely claiming no GPS.
                    city = city or gps_cities.get((path.parent, taken.date())) or 'GPS位置未知'
                    stem = f'{prefix}_{taken:%Y%m%d}_{city}_{sequences[year_key]:04d}'
                else:
                    stem = f'{prefix}_{taken:%Y%m%d}_{taken:%H-%M-%S}_{sequences[year_key]:04d}'
                target = make_target(path, stem, ext, reserved)
                old, new = str(path), str(target)
                path.rename(target)
                stats['renamed'] += 1
                # Log these categories according to the actual new filename,
                # not according to whether EXIF contains GPS coordinates.
                if '无GPS' in target.name:
                    stats['no_gps_renames'].append((old, new))
                if 'GPS位置未知' in target.name:
                    stats['gps_unknown_renames'].append((old, new))
                ext_stat = stats['extension_stats'].get(path.suffix.casefold())
                if ext_stat:
                    ext_stat['renamed'] += 1
                if debug_log:
                    stats['renames'].append((old, new))
                if log_cb: log_cb(f'[已改名] {old} -> {new}')
        except (OSError, ValueError, UnidentifiedImageError) as error:
            stats['errors'].append(f'{path}：{error}')
            if log_cb: log_cb(f'[失败] {path}：{error}')
        if progress_cb: progress_cb(index, total, path.name)
    for item in stats['extension_stats'].values():
        item['not_renamed'] = item['total'] - item['renamed']
    stats['elapsed'] = time.perf_counter() - started
    stats['log_path'] = write_log(root, stats)
    return stats


class PhotoRenameApp:
    def __init__(self, root):
        self.root = root
        root.title('PhotoRename 照片重新命名工具')
        root.geometry('780x600')
        root.minsize(700, 520)
        # Bring the application to the foreground at launch, but do not keep
        # it permanently above dialogs such as the directory chooser.
        root.attributes('-topmost', True)
        root.lift()
        root.focus_force()
        root.after(250, lambda: root.attributes('-topmost', False))
        self.queue = queue.Queue()
        self.stop_flag = threading.Event()
        self.debug_log = tk.BooleanVar(value=False)
        self.build_ui()
        root.after(100, self.poll_queue)

    def build_ui(self):
        font = ('Microsoft YaHei UI', 10)
        tk.Label(self.root, text='PhotoRename 照片重新命名工具', font=(font[0], 16, 'bold')).pack(pady=(16, 4))
        tk.Label(self.root, text='根据照片 EXIF 的制造商、型号和拍摄时间递归重新命名', fg='#666').pack(pady=(0, 12))
        frame = tk.Frame(self.root); frame.pack(fill='x', padx=16, pady=4)
        tk.Label(frame, text='图片库目录：', width=14, anchor='w', font=font).pack(side='left')
        self.library = tk.StringVar(); tk.Entry(frame, textvariable=self.library, font=font).pack(side='left', fill='x', expand=True, padx=(0, 8))
        tk.Button(frame, text='浏览', width=8, command=self.choose).pack(side='left')
        self.progress = ttk.Progressbar(self.root, mode='determinate'); self.progress.pack(fill='x', padx=16, pady=(16, 4))
        self.status = tk.StringVar(value='就绪 - 请选择图片库目录'); tk.Label(self.root, textvariable=self.status, fg='#1a6fc4').pack(pady=(2, 8))
        buttons = tk.Frame(self.root); buttons.pack(fill='x', padx=16, pady=(0, 8))
        self.start = tk.Button(buttons, text='开始重新命名', font=(font[0], 13, 'bold'), bg='#4caf50', fg='white', height=2, command=self.start_work); self.start.pack(side='left', fill='x', expand=True, padx=(0, 6))
        self.stop = tk.Button(buttons, text='停止', font=(font[0], 13), height=2, width=8, state='disabled', command=self.stop_work); self.stop.pack(side='left')
        tk.Checkbutton(self.root, text='调试日志：记录旧名称/新名称明细（默认关闭）', variable=self.debug_log).pack(anchor='w', padx=16, pady=(0, 8))
        box = tk.Frame(self.root); box.pack(fill='both', expand=True, padx=16, pady=(0, 16))
        self.log = tk.Text(box, font=('Consolas', 9), wrap='none'); self.log.pack(side='left', fill='both', expand=True)
        tk.Scrollbar(box, command=self.log.yview).pack(side='right', fill='y')

    def choose(self):
        selected = filedialog.askdirectory(title='选择图片库目录')
        if selected: self.library.set(selected)

    def start_work(self):
        root = self.library.get().strip()
        if not os.path.isdir(root): return messagebox.showerror('错误', '请选择有效的图片库目录')
        if not messagebox.askyesno('确认', '将按 EXIF 信息重新命名符合条件的照片，是否继续？'): return
        self.log.delete('1.0', 'end'); self.start.config(state='disabled'); self.stop.config(state='normal'); self.stop_flag.clear()
        threading.Thread(target=self.worker, args=(root,), daemon=True).start()

    def stop_work(self): self.stop_flag.set(); self.status.set('正在停止...'); self.stop.config(state='disabled')

    def worker(self, root):
        try:
            stats = rename_photos(root, lambda d, t, n: self.queue.put(('progress', d, t, n)), lambda s: self.queue.put(('log', s)), self.stop_flag, self.debug_log.get())
            self.queue.put(('done', stats))
        except Exception as error: self.queue.put(('error', str(error)))

    def poll_queue(self):
        try:
            while True:
                item = self.queue.get_nowait()
                if item[0] == 'progress':
                    _, done, total, name = item; self.progress['maximum'] = total; self.progress['value'] = done; self.status.set(f'处理进度 ({done}/{total})：{name}')
                elif item[0] == 'log': self.log.insert('end', item[1] + '\n'); self.log.see('end')
                elif item[0] == 'done':
                    stats = item[1]; self.start.config(state='normal'); self.stop.config(state='disabled')
                    if stats.get('no_work'):
                        self.status.set('完成 - 最新日志中没有待重新命名的 GPS 文件')
                        messagebox.showinfo('无需处理', '最新日志中没有“无GPS”或“GPS位置未知”的文件列表，无需重新命名。')
                    else:
                        self.status.set(f"完成 - 成功重新命名 {stats['renamed']} 个文件")
                        messagebox.showinfo('处理完成', f"扫描文件：{stats['scanned']}\n成功重新命名：{stats['renamed']}\n跳过：{stats['skipped']}\n失败：{len(stats['errors'])}\n\n日志：{stats['log_path']}")
                elif item[0] == 'error': self.start.config(state='normal'); self.stop.config(state='disabled'); messagebox.showerror('错误', item[1])
        except queue.Empty: pass
        self.root.after(100, self.poll_queue)


if __name__ == '__main__':
    root = tk.Tk(); PhotoRenameApp(root); root.mainloop()
