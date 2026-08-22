import tempfile
import unittest
from pathlib import Path

from PIL import Image, PngImagePlugin

import photodedup


class PhotoDedupOtherImageTests(unittest.TestCase):
    def test_deduplicates_identical_pngs_across_tidy_categories(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            library_dir = Path(temp_dir)
            year_dir = library_dir / '2026年照片集'
            other_dir = year_dir / '其他图片文件'
            screenshot_dir = year_dir / '截图类文件'
            other_dir.mkdir(parents=True)
            screenshot_dir.mkdir()

            image = Image.new('RGB', (24, 24), color=(20, 40, 60))
            first = other_dir / '转发图片.png'
            second = screenshot_dir / '截图_001.png'
            image.save(first)
            image.save(second)

            stats = photodedup.run_dedup(str(library_dir), threshold=1)

            self.assertEqual(stats['total_images'], 2)
            self.assertEqual(stats['total_dup_count'], 1)
            self.assertEqual(
                stats['top_level_results']['2026年照片集']['global_md5_dup_count'],
                1,
            )
            self.assertEqual(len(list((library_dir / photodedup.DUP_DIR_NAME).rglob('*.png'))), 1)
            self.assertEqual(sum(path.exists() for path in (first, second)), 1)

    def test_visually_deduplicates_pngs_with_different_metadata(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            library_dir = Path(temp_dir)
            year_dir = library_dir / '2026年照片集'
            other_dir = year_dir / '其他图片文件'
            screenshot_dir = year_dir / '截图类文件'
            other_dir.mkdir(parents=True)
            screenshot_dir.mkdir()

            image = Image.new('RGB', (24, 24), color=(20, 40, 60))
            image.save(other_dir / '转发图片.png')
            metadata = PngImagePlugin.PngInfo()
            metadata.add_text('Source', 'screenshot copy')
            image.save(screenshot_dir / '截图_001.png', pnginfo=metadata)

            stats = photodedup.run_dedup(str(library_dir), threshold=1)

            self.assertEqual(stats['total_dup_count'], 1)
            self.assertEqual(
                stats['top_level_results']['2026年照片集']['phash_dup_count'],
                1,
            )


if __name__ == '__main__':
    unittest.main()
