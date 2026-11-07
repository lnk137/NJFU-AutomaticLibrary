"""Exercise generated mail at the SMTP boundary without any network access."""
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'AL-BackEnd'))
from local_app.mail_config import MailConfig, SMTPSender


class MailHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []
        self.links = []
        self.text = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        if tag == 'a':
            self.links.append(dict(attrs))

    def handle_data(self, data):
        self.text.append(data)


class MailRenderingTests(unittest.TestCase):
    def setUp(self):
        self.messages = []
        self.connections = 0
        owner = self

        class FakeSMTP:
            def __init__(self, *args, **kwargs):
                owner.connections += 1

            def login(self, username, password):
                pass

            def send_message(self, message, *, from_addr, to_addrs):
                owner.messages.append(BytesParser(policy=policy.default).parsebytes(message.as_bytes()))
                return {}

            def close(self):
                pass

        self.smtp_patch = patch('local_app.mail_config.smtplib.SMTP_SSL', FakeSMTP)
        self.smtp_patch.start()
        self.addCleanup(self.smtp_patch.stop)
        self.sender = SMTPSender(MailConfig('smtp.example.com', 465, 'tls',
            'fixture-user', 'fixture-password', 'from@example.com'))

    def envelope(self, **changes):
        return {'to': 'receiver@example.com', 'subject': '图书馆预约使用提醒',
            'body': '使用时段：2026-11-06 18:00:00–2026-11-06 18:10:00\n座位：A001',
            'message_id': '<fixture@local.invalid>', **changes}

    def test_action_mail_is_multipart_with_exact_plain_fallback_and_button(self):
        url = 'http://127.0.0.1:5003/static/mail-cancel.html#ticket=fixture-ticket'
        body = self.envelope()['body'] + '\n' + url + '\n此链接仅能在运行预约后台的电脑上打开。'
        summary = self.envelope()['body'] + '\n此链接仅能在运行预约后台的电脑上打开。'
        self.sender(self.envelope(body=body, html_body=summary, action={'label': '取消预约', 'url': url}))
        message = self.messages[0]
        self.assertEqual(message.get_content_type(), 'multipart/alternative')
        self.assertEqual([part.get_content_type() for part in message.iter_parts()], ['text/plain', 'text/html'])
        self.assertEqual(message.get_body(preferencelist=('plain',)).get_content(), body + '\n')
        html_part = message.get_body(preferencelist=('html',))
        self.assertIsNotNone(html_part)
        html = html_part.get_content()
        parsed = MailHTMLParser(); parsed.feed(html)
        self.assertEqual(len(parsed.links), 1)
        self.assertEqual(parsed.links[0]['href'], url)
        self.assertIn('取消预约', ''.join(parsed.text))
        self.assertNotIn(url, ''.join(parsed.text))
        self.assertIn('background', parsed.links[0].get('style', ''))
        self.assertNotIn('此链接仅能', parsed.links[0]['href'])
        self.assertIn('此链接仅能在运行预约后台的电脑上打开。', ''.join(parsed.text))
        for forbidden in ('script', 'img', 'iframe', 'link'):
            self.assertNotIn(forbidden, parsed.tags)

    def test_html_escapes_body_subject_action_label_and_href(self):
        url = 'https://library.example.com:5443/#/pages/reservation_info/reservation_info?account_id=a&record_id="onclick="<bad>&intent=cancel'
        body = '<script>steal()</script>\n座位：<img src=x onerror=steal()> & "注释"'
        label = '查看 <img src=x> & "取消"'
        subject = '提醒 <script>alert(1)</script>'
        self.sender(self.envelope(subject=subject, body=body, action={'label': label, 'url': url}))
        message = self.messages[0]
        self.assertEqual(message['Subject'], subject)
        self.assertEqual(message.get_body(preferencelist=('plain',)).get_content(), body + '\n')
        html_part = message.get_body(preferencelist=('html',))
        self.assertIsNotNone(html_part)
        html = html_part.get_content()
        parsed = MailHTMLParser(); parsed.feed(html)
        self.assertEqual([link['href'] for link in parsed.links], [url])
        self.assertEqual(set(parsed.links[0]) - {'href', 'style'}, set())
        self.assertNotIn('script', parsed.tags)
        self.assertNotIn('img', parsed.tags)
        self.assertIn(body.split('\n')[0], ''.join(parsed.text))
        self.assertIn(label, ''.join(parsed.text))
        self.assertIn('&lt;script&gt;', html)
        self.assertIn('&amp;record_id=&quot;onclick=&quot;&lt;bad&gt;', html)

    def test_envelope_without_action_stays_plain_text(self):
        for subject in ('邮箱验证码', '预约成功', '预约失败', '预约结果待确认', '预约已取消'):
            with self.subTest(subject=subject):
                self.sender(self.envelope(subject=subject))
                message = self.messages[-1]
                self.assertEqual(message.get_content_type(), 'text/plain')
                self.assertIsNone(message.get_body(preferencelist=('html',)))
                self.assertEqual(message.get_content(), self.envelope()['body'] + '\n')

    def test_unsafe_action_links_fail_before_any_smtp_connection(self):
        urls = ['javascript:alert(1)', 'data:text/html,<script>alert(1)</script>',
            'file:///etc/passwd', 'mailto:receiver@example.com', '//library.example.com/path',
            '/#/pages/reservation_info/reservation_info', 'https:///missing-host',
            'https://library.example.com/\r\nBcc:other@example.com',
            'https://library.example.com/%0d%0aBcc:other@example.com',
            ' https://library.example.com/path', 'https://user:pass@library.example.com/path']
        for url in urls:
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.sender(self.envelope(action={'label': '查看并取消预约', 'url': url}))
        self.assertEqual(self.connections, 0)
        self.assertEqual(self.messages, [])


if __name__ == '__main__':
    unittest.main()
