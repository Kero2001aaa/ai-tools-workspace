"""Arabic presentation must not change API/action/schema contracts."""
import re
import unittest
from agent.control_plane import _page


class ArabicControlPlaneTests(unittest.TestCase):
    def test_arabic_rtl_and_isolated_machine_output(self):
        page=_page()
        self.assertIn('<html lang="ar" dir="rtl">',page)
        self.assertIn('تقرير اقتباسات الكود المحفوظ',page)
        self.assertIn('ليست الكود الحالي أو فهمًا بنموذج',page)
        self.assertIn('pre,input{direction:ltr;text-align:left;unicode-bidi:plaintext}',page)
        self.assertIn('dir="auto"',page)
        self.assertEqual(['workspace_status','memory_recent'],re.findall(r'<option value="([^"]+)"',page))
        self.assertIn("Authorization:'Bearer '+token",page)
        self.assertIn("output.textContent=lines.join",page)
        self.assertNotIn('localStorage',page)
