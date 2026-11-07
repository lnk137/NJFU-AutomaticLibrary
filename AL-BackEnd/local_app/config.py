"""Explicit local configuration, independent of legacy import side effects."""
from dataclasses import dataclass, field
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit
from .models import AppError


@dataclass(frozen=True)
class Settings:
    root: Path
    data_dir: Path
    mongo_uri: str = 'mongodb://127.0.0.1:27019/'
    database_name: str = 'AutoLib'
    port: int = 5003
    allow_live: bool = False
    public_origin: str | None = None
    relay_token: str | None = field(default=None, repr=False)
    executor: str = 'python'
    executor_release: str | None = field(default=None,repr=False)


def public_overrides(path):
    """Explicit private config; no secret in arguments or environment variables."""
    def read_private(file):
        file=Path(file)
        if file.is_symlink() or not file.is_file() or file.stat().st_uid!=os.getuid() or file.stat().st_mode & 0o077:
            raise AppError('unsafe_public_config','公网配置及转发凭据须为当前用户的0600文件')
        value=file.read_text()
        if len(value)>8192:raise AppError('unsafe_public_config','公网配置过大')
        return value
    try:
        config=json.loads(read_private(path))
        if set(config)!={'origin','relay_token_file'}:raise ValueError()
        return {'public_origin':config['origin'],'relay_token':read_private(config['relay_token_file']).strip()}
    except (ValueError,TypeError,OSError,KeyError):
        raise AppError('invalid_public_config','公网配置无法安全读取') from None


def load_settings(root, overrides=None):
    root = Path(root).resolve()
    values = dict(root=root, data_dir=root / '.local-data') | (overrides or {})
    if 'executor' not in (overrides or {}):
        from .native.selection import selection_values
        values.update(selection_values(values['data_dir']))
    settings = Settings(**values)
    if settings.executor not in ('python','cpp'):
        raise AppError('invalid_executor','执行器选择无效')
    uri = urlsplit(settings.mongo_uri)
    if (uri.scheme != 'mongodb' or uri.hostname not in {'127.0.0.1', 'localhost', '::1'}
            or ',' in uri.netloc or uri.username or uri.query or uri.fragment or uri.path not in ('', '/')):
        raise AppError('invalid_database', '只允许显式的本机 MongoDB 地址')
    if type(settings.port) is not int or not 1 <= settings.port <= 65535 or type(settings.allow_live) is not bool:
        raise AppError('invalid_settings', '本机端口或执行开关无效')
    if settings.public_origin is not None:
        try:
            public=urlsplit(settings.public_origin)
            valid=(public.scheme=='https' and public.hostname and public.hostname not in {'127.0.0.1','localhost','::1'}
                   and not public.username and not public.password and not public.path and not public.query and not public.fragment
                   and public.port is not None and settings.public_origin=='https://'+public.netloc
                   and re.fullmatch(r'[a-z0-9.-]+:[0-9]+',public.netloc)
                   and isinstance(settings.relay_token,str) and re.fullmatch(r'[0-9a-f]{64}',settings.relay_token))
        except (ValueError,TypeError):valid=False
        if not valid:raise AppError('invalid_public_config','须明确提供HTTPS域名、端口及安全转发凭据')
    elif settings.relay_token is not None:raise AppError('invalid_public_config','转发凭据必须绑定明确公网入口')
    return settings
