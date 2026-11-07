"""HTML parser checks for the viewport under the application's no-inline-script CSP."""
from html.parser import HTMLParser
from pathlib import Path
import unittest


class Document(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.viewports = []
        self.inline_scripts = []
        self.script = None
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'meta' and attrs.get('name') == 'viewport':
            self.viewports.append(attrs.get('content', ''))
        if tag == 'script':
            self.script = {'src': attrs.get('src'), 'body': ''}

    def handle_data(self, data):
        if self.script is not None:
            self.script['body'] += data

    def handle_endtag(self, tag):
        if tag == 'script' and self.script is not None:
            if not self.script['src'] and self.script['body'].strip():
                self.inline_scripts.append(self.script['body'])
            self.script = None


class ViewportTests(unittest.TestCase):
    def setUp(self):
        source = Path(__file__).resolve().parents[1] / 'AL-FrontEnd/index.html'
        self.document = Document(source.read_text())

    def test_phone_viewport_exists_before_any_script_can_run(self):
        self.assertEqual(len(self.document.viewports), 1)
        values = dict(
            item.strip().split('=', 1)
            for item in self.document.viewports[0].split(',')
            if '=' in item
        )
        self.assertEqual(values['width'], 'device-width')
        self.assertEqual(float(values['initial-scale']), 1.0)

    def test_required_head_setup_does_not_depend_on_blocked_inline_javascript(self):
        self.assertEqual(self.document.inline_scripts, [])

    def test_viewport_does_not_disable_accessible_zoom(self):
        self.assertEqual(len(self.document.viewports), 1)
        content = self.document.viewports[0]
        self.assertNotIn('user-scalable=no', content)
        self.assertNotIn('maximum-scale=1', content)


if __name__ == '__main__':
    unittest.main()
