"""B9（線色統一）: PDF側（部門レポート・病院サマリ）に旧オレンジ`#E69F00`が無いことの回帰テスト。

改修プラン_訴求力強化.md F5／B9裁定（`#2b6cb0`に統一。PDF側は既にこの色が「本丸」で
変更不要）。Web側（detail.html/dept.html）のB9対応は別バッチ（Phase 2 batch2-2）の担当
のため、このテストは PDF 生成に関わるファイル（app/lib/dept_report.py、
app/templates/dept_report.html、app/lib/hospital_summary.py）のみを対象にする。

実行: リポジトリルートで
    python -m pytest tests/test_pdf_line_color_consistency.py -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.lib import dept_report as dr  # noqa: E402
from app.lib import hospital_summary as hs  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PDF_SIDE_FILES = [
    ROOT / "app" / "lib" / "dept_report.py",
    ROOT / "app" / "templates" / "dept_report.html",
    ROOT / "app" / "lib" / "hospital_summary.py",
]


class NoOrangeInPdfSideFilesTest(unittest.TestCase):
    def test_no_e69f00_in_pdf_side_files(self):
        for path in PDF_SIDE_FILES:
            with self.subTest(path=path.name):
                self.assertNotIn("E69F00", path.read_text(encoding="utf-8"))

    def test_part_line_constant_is_unified_color(self):
        self.assertEqual(dr.PART_LINE, "#2b6cb0")

    def test_hospital_summary_line_constant_is_unified_color(self):
        self.assertEqual(hs.LINE, "#2b6cb0")


if __name__ == "__main__":
    unittest.main()
