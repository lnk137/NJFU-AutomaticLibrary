"""Official seat payload observed on 2026-10-20; no network or saved secrets."""
import base64
import binascii
import re
import secrets
import string
from Crypto.Cipher import AES, PKCS1_v1_5
from Crypto.PublicKey import RSA
from .models import AppError


def seat_fields(seat_id, public_key):
    if not isinstance(seat_id, str) or not re.fullmatch(r'[1-9][0-9]{0,15}', seat_id):
        raise AppError('school_protocol', '座位编号格式异常，未提交预约', 502)
    try:
        if not isinstance(public_key, str) or not 1 <= len(public_key) <= 8192:
            raise ValueError()
        raw = public_key.strip()
        key = RSA.import_key(raw if raw.startswith('-----BEGIN PUBLIC KEY-----')
                             else base64.b64decode(raw, validate=True))
        if key.has_private() or not 1024 <= key.size_in_bits() <= 4096:
            raise ValueError()
    except (ValueError, TypeError, IndexError, binascii.Error):
        raise AppError('school_protocol', '学校预约公钥格式变化，未提交预约', 502) from None
    alphabet = string.ascii_uppercase + string.ascii_lowercase + string.digits
    aes_key = ''.join(secrets.choice(alphabet) for _ in range(16)).encode('ascii')
    plain = seat_id.encode('utf-8')
    # fa7d.a: AES-CBC, ZeroPadding, fixed public protocol IV, uppercase hex.
    plain += b'\0' * (-len(plain) % AES.block_size)
    encrypted = AES.new(aes_key, AES.MODE_CBC, b'ABCDEF1234123412').encrypt(plain)
    return {'resvDev': [0],
            'encryptVlaue': base64.b64encode(PKCS1_v1_5.new(key).encrypt(aes_key)).decode('ascii'),
            'resvDevEncrypts': [encrypted.hex().upper()]}


def safe_message(value, default='学校未接受本次预约'):
    if not isinstance(value, str) or not value.strip():
        return default
    message = ' '.join(value.split())
    message = re.sub(r'https?://\S+', '[url]', message)
    message = re.sub(r'(?i)\b(password|passwd|pwd|token|ticket|cookie|secret|authorization)\b\s*[=:]\s*[^\s,;]+',
                     r'\1=[redacted]', message)
    message = re.sub(r'密码\s*[=:：]\s*[^\s,;，；]+', '密码=[redacted]', message)
    message = re.sub(r'[A-Za-z0-9_+/=-]{32,}', '[redacted]', message)
    return message[:300]
