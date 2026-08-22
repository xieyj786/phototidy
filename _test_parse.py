import importlib.util
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from unittest.mock import MagicMock

# 使用与测试文件同目录的 photorename.py，避免硬编码绝对路径。
SCRIPT = Path(__file__).resolve().parent / 'photorename.py'

# spec_from_file_location 在无法定位文件时返回 None，spec.loader 也可能为 None，
# 因此使用前必须先判空，否则 Pylance 会报 “exec_module” 不是 “None” 的已知属性。
spec = importlib.util.spec_from_file_location('photorename', SCRIPT)
if spec is None or spec.loader is None:
    raise ImportError(f'无法从 {SCRIPT} 创建模块规格')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

parse_datetime = module.parse_datetime
read_exif = module.read_exif
city_from_gps = module.city_from_gps


class ParseDatetimeTests(unittest.TestCase):
    """photorename.parse_datetime 解析测试。"""

    cases = [
        ('2024:08:12 09:30:45', datetime(2024, 8, 12, 9, 30, 45)),        # EXIF 冒号日期 + 空格
        ('2024-08-12 09:30:45', datetime(2024, 8, 12, 9, 30, 45)),        # 短横线日期 + 空格
        ('2024:08:12T09:30:45', datetime(2024, 8, 12, 9, 30, 45)),        # EXIF 冒号日期 + T 分隔
        ('2024-08-12T09:30:45', datetime(2024, 8, 12, 9, 30, 45)),        # ISO 风格
        ('2024:08:12 09:30:45.000', datetime(2024, 8, 12, 9, 30, 45)),    # 毫秒尾缀应被忽略
        (' 2024:08:12 09:30:45  ', datetime(2024, 8, 12, 9, 30, 45)),     # 首尾空白
        ('\x002024:08:12 09:30:45', datetime(2024, 8, 12, 9, 30, 45)),    # 空字节应被清理
        ('2024:02:29 12:00:00', datetime(2024, 2, 29, 12, 0, 0)),         # 闰年
    ]

    invalid_cases = [
        None,
        '',
        '   ',
        '2024/08/12 09:30:45',      # 斜杠分隔不支持
        '2024-08-12',               # 缺少时间
        '2024-08-12 09:30',         # 缺少秒
        '2024-13-12 09:30:45',      # 月份越界
        '2024-00-12 09:30:45',      # 月份为 0
        '2024-08-32 09:30:45',      # 日期越界
        '2024-08-12 24:00:00',      # 小时越界
        '2024:08:12 09:30:61',      # 秒越界
        'abcd:ef:gh ij:kl:mn',      # 非数字
    ]

    def test_valid_cases(self):
        for raw, expected in self.cases:
            with self.subTest(raw=raw):
                self.assertEqual(parse_datetime(raw), expected)

    def test_invalid_cases(self):
        for raw in self.invalid_cases:
            with self.subTest(raw=raw):
                self.assertIsNone(parse_datetime(raw))


class ReadExifTests(unittest.TestCase):
    def test_does_not_use_exif_datetime_as_capture_time(self):
        fake_image = unittest.mock.MagicMock()
        fake_image.__enter__.return_value.getexif.return_value = {
            271: 'Test',
            272: 'Camera',
            306: '2026:08:13 12:00:00',
        }
        with patch.object(module.Image, 'open', return_value=fake_image):
            self.assertEqual(read_exif('photo.jpg'), ('Test', 'Camera', None))

    def test_ignores_non_mapping_gps_exif(self):
        fake_image = unittest.mock.MagicMock()
        fake_image.__enter__.return_value.getexif.return_value = {
            271: 'Test', 272: 'Camera', 36867: '2026:08:13 12:00:00',
            34853: 1,
        }
        with patch.object(module.Image, 'open', return_value=fake_image):
            self.assertEqual(
                read_exif('photo.jpg', include_gps=True),
                ('Test', 'Camera', datetime(2026, 8, 13, 12, 0, 0), None),
            )

    def test_reads_capture_time_from_exif_sub_ifd(self):
        fake_image = unittest.mock.MagicMock()
        fake_exif = unittest.mock.MagicMock()
        fake_exif.get.side_effect = {271: '', 272: ''}.get
        fake_exif.get_ifd.side_effect = lambda tag: {
            0x8769: {36867: '2026:08:13 12:00:00'},
            0x8825: {},
        }.get(tag, {})
        fake_image.__enter__.return_value.getexif.return_value = fake_exif
        with patch.object(module.Image, 'open', return_value=fake_image):
            self.assertEqual(
                read_exif('photo.jpg'),
                ('', '', datetime(2026, 8, 13, 12, 0, 0)),
            )

    def test_reads_gps_from_exif_sub_ifd(self):
        fake_image = unittest.mock.MagicMock()
        fake_exif = unittest.mock.MagicMock()
        fake_exif.get.side_effect = {
            271: 'Test', 272: 'Camera', 36867: '2026:08:13 12:00:00',
        }.get
        fake_exif.get_ifd.return_value = {
            1: 'N', 2: (1, 2, 3), 3: 'E', 4: (4, 5, 6),
        }
        fake_image.__enter__.return_value.getexif.return_value = fake_exif
        with patch.object(module.Image, 'open', return_value=fake_image):
            result = read_exif('photo.jpg', include_gps=True)
        self.assertEqual(result[:3], ('Test', 'Camera', datetime(2026, 8, 13, 12, 0, 0)))
        self.assertAlmostEqual(result[3][0], 1.034166, places=5)
        self.assertAlmostEqual(result[3][1], 4.085, places=5)

    def test_city_lookup_falls_back_to_country_name(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b''
        with patch.object(module.urllib.request, 'urlopen', return_value=response), \
             patch.object(module.json, 'load', return_value={'address': {'country': 'South Africa'}}):
            self.assertEqual(city_from_gps((1.0, 2.0)), '\u5357\u975e')


if __name__ == '__main__':
    unittest.main()
