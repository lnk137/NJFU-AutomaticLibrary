"""Optional private SMTP configuration and a TLS-only, bounded sender."""
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr
from html import escape
import json
import os
from pathlib import Path
import re
import smtplib
import socket
import ssl
import stat
import threading
from urllib.parse import unquote, urlsplit


def email_address(value):
    if (not isinstance(value, str) or len(value) > 254 or value != value.strip()
            or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}", value)
            or '..' in value or len(value.split('@')[0]) > 64):
        raise ValueError('Invalid email address')
    return value


@dataclass(frozen=True)
class MailConfig:
    host: str
    port: int
    security: str
    username: str = field(repr=False)
    password: str = field(repr=False)
    from_address: str
    from_name: str = '图书馆预约'


def load_mail_config(data_dir):
    """Missing/disabled is normal; every failure returns a redacted status code."""
    path = Path(data_dir) / 'mail.json'
    try:
        if path.is_symlink() or Path(data_dir).is_symlink():
            return None, 'unsafe_mail_config'
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None, None
        with os.fdopen(fd, 'rb') as handle:
            info = os.fstat(handle.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 16384):
                return None, 'unsafe_mail_config'
            value = json.loads(handle.read(16385))
        if not isinstance(value, dict) or type(value.get('enabled', False)) is not bool:
            raise ValueError()
        allowed = {'enabled', 'host', 'port', 'security', 'username', 'password', 'from_address', 'from_name'}
        if set(value) - allowed:
            raise ValueError()
        if not value.get('enabled', False):
            return None, None
        config = MailConfig(**{key: val for key, val in value.items() if key != 'enabled'})
        if (not isinstance(config.host, str) or not re.fullmatch(r'[A-Za-z0-9.-]{1,253}', config.host)
                or type(config.port) is not int or not 1 <= config.port <= 65535
                or config.security not in ('starttls', 'tls')
                or not isinstance(config.username, str) or not 1 <= len(config.username) <= 320
                or not isinstance(config.password, str) or not 1 <= len(config.password) <= 2048
                or not isinstance(config.from_name, str) or len(config.from_name) > 80
                or any(char in config.from_name for char in '\r\n')):
            raise ValueError()
        email_address(config.from_address)
        return config, None
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        return None, 'invalid_mail_config'


def compose_message(config, envelope):
    """Build MIME locally; validate optional reminder actions before transport."""
    action = envelope.get('action')
    if action is not None:
        if (not isinstance(action, dict) or set(action) != {'label', 'url'}
                or not isinstance(action['label'], str) or not action['label']
                or not isinstance(action['url'], str) or not action['url']):
            raise ValueError('Invalid email action')
        url = action['url']
        try:
            parsed = urlsplit(url)
            valid = (parsed.scheme in ('http', 'https') and parsed.hostname
                     and not parsed.username and not parsed.password
                     and not any(char.isspace() or ord(char) < 32 for char in url)
                     and '\\' not in url and not any(char in unquote(url) for char in '\r\n'))
            parsed.port
        except ValueError:
            valid = False
        if not valid:
            raise ValueError('Invalid email action URL')
    message = EmailMessage()
    message['From'] = formataddr((config.from_name, config.from_address))
    message['To'] = email_address(envelope['to'])
    message['Subject'] = envelope['subject']
    message['Message-ID'] = envelope['message_id']
    message.set_content(envelope['body'])
    if action is not None:
        subject = escape(envelope['subject'])
        body = escape(envelope.get('html_body', envelope['body'])).replace('\n', '<br>\n')
        label, href = escape(action['label']), escape(action['url'], quote=True)
        message.add_alternative(
            '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            f'<title>{subject}</title></head>'
            '<body style="margin:0;padding:24px;background:#f4f6f8;color:#202936;'
            'font-family:Arial,sans-serif;line-height:1.7">'
            '<main style="max-width:620px;margin:0 auto;padding:28px;background:#ffffff;'
            'border:1px solid #dce2e8;border-radius:12px">'
            f'<h1 style="margin:0 0 20px;font-size:22px;line-height:1.4">{subject}</h1>'
            f'<p style="margin:0 0 24px;overflow-wrap:anywhere">{body}</p>'
            f'<p style="margin:0"><a href="{href}" style="display:inline-block;'
            'padding:14px 24px;border-radius:8px;background:#1b604c;color:#ffffff;'
            f'font-weight:bold;text-decoration:none">{label}</a></p>'
            '</main></body></html>', subtype='html')
    return message


class SMTPSender:
    """A send attempt is never repeated after DATA or an ambiguous disconnect."""
    def __init__(self, config):
        self.config = config
        self._guard = threading.Lock()
        self._connection = None
        self._closed = False

    def close(self):
        with self._guard:
            self._closed = True
            connection = self._connection
            if connection is not None and connection.sock is not None:
                try:
                    connection.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def __call__(self, envelope):
        if self._closed:
            raise OSError('SMTP sender closed')
        config = self.config
        message = compose_message(config, envelope)
        recipient = str(message['To'])
        context = ssl.create_default_context()
        connection = None
        try:
            if config.security == 'tls':
                connection = smtplib.SMTP_SSL(config.host, config.port, timeout=5, context=context)
            else:
                connection = smtplib.SMTP(config.host, config.port, timeout=5)
            with self._guard:
                self._connection = connection
                if self._closed:
                    raise OSError('SMTP sender closed')
            if config.security == 'starttls':
                connection.ehlo()
                connection.starttls(context=context)
                connection.ehlo()
            connection.login(config.username, config.password)
            refused = connection.send_message(message, from_addr=config.from_address, to_addrs=[recipient])
            if refused:
                raise smtplib.SMTPRecipientsRefused(refused)
        finally:
            # QUIT can time out after accepted DATA; it cannot change acceptance.
            if connection is not None:
                connection.close()
            with self._guard:
                self._connection = None
