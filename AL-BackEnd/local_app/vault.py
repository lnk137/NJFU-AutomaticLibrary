"""Authenticated local credential storage; key never travels in API responses."""
import base64
import os
from pathlib import Path
import stat
from Crypto.Cipher import AES
from .models import AppError


class CredentialVault:
    def __init__(self, key): self.key = key

    @classmethod
    def open(cls, path, *, has_encrypted_accounts):
        path = Path(path)
        if path.parent.is_symlink(): raise AppError('unsafe_key', '密钥目录不能是符号链接', 503)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.parent.stat().st_mode & 0o077: raise AppError('unsafe_key', '密钥目录权限须为 0700', 503)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            if has_encrypted_accounts: raise AppError('missing_key', '加密密钥缺失，原账号数据已保留；请恢复原密钥', 503) from None
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, 'wb') as handle:
                    handle.write(os.urandom(32));handle.flush();os.fsync(handle.fileno())
            except FileExistsError: pass
            return cls.open(path, has_encrypted_accounts=True)
        except OSError: raise AppError('unsafe_key', '密钥文件不可安全读取', 503) from None
        with os.fdopen(fd, 'rb') as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise AppError('unsafe_key', '密钥文件必须属于当前用户且权限为 0600', 503)
            key = handle.read(33)
        if len(key) != 32: raise AppError('unsafe_key', '密钥文件无效，未替换原文件', 503)
        return cls(key)

    def seal(self, account_id, password):
        cipher = AES.new(self.key, AES.MODE_GCM)
        cipher.update(account_id.encode())
        data, tag = cipher.encrypt_and_digest(password.encode('utf-8'))
        return {k: base64.b64encode(v).decode() for k, v in {'nonce': cipher.nonce, 'ciphertext': data, 'tag': tag}.items()}

    def unseal(self, account_id, envelope):
        try:
            values = {k: base64.b64decode(envelope[k], validate=True) for k in ('nonce', 'ciphertext', 'tag')}
            cipher = AES.new(self.key, AES.MODE_GCM, nonce=values['nonce']);cipher.update(account_id.encode())
            return cipher.decrypt_and_verify(values['ciphertext'], values['tag']).decode('utf-8')
        except (ValueError, KeyError, TypeError, UnicodeError):
            raise AppError('credential_unavailable', '账号凭据无法解密，请核对原密钥', 503) from None
