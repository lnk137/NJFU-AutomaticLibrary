"""Private opt-in pins the tested adapter and binary across existing launchers."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from ..models import AppError
from .build import digest,private_directory,read_build


def release_identity(root):
    root=Path(root)
    paths=[*sorted((root/'AL-BackEnd/local_app').rglob('*.py')),
           *sorted((root/'AL-BackEnd/native_executor').iterdir()),
           *(root/name for name in ('AL-BackEnd/main.py','local_run.py','public_run.py','power_agent.py'))]
    sources={}
    for path in paths:
        if path.is_symlink() or not path.is_file():raise AppError('native_release_invalid','执行器安装内容无法核对',503)
        sources[str(path.relative_to(root))]=digest(path)
    return hashlib.sha256(json.dumps(sources,sort_keys=True).encode()).hexdigest(),sources


def selection_values(data_dir):
    path=Path(data_dir)/'native-selection.json'
    if not path.exists() and not path.is_symlink():
        artifacts=[Path(data_dir)/name for name in ('native','native-state.json')]
        return {'executor':'cpp','executor_release':'invalid'} if any(p.exists() or p.is_symlink() for p in artifacts) else {}
    try:
        info=path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077
                or info.st_nlink!=1 or info.st_size>131072):raise ValueError()
        value=json.loads(path.read_text())
        if value=={'schema':1,'executor':'python'}:return {'executor':'python'}
        if (set(value)!={'schema','executor','release_sha256','sources','binary_sha256'}
                or value['schema']!=1 or value['executor']!='cpp'
                or not all(isinstance(value[key],str) and re.fullmatch('[0-9a-f]{64}',value[key])
                           for key in ('release_sha256','binary_sha256'))
                or not isinstance(value['sources'],dict)):raise ValueError()
        return {'executor':'cpp','executor_release':value['release_sha256']}
    except (OSError,ValueError,TypeError,KeyError):
        # Never turn a corrupted opt-in into a second/unpinned Python sender.
        return {'executor':'cpp','executor_release':'invalid'}


def verify_selection(settings):
    values=selection_values(settings.data_dir)
    identity,sources=release_identity(settings.root)
    if values!={'executor':'cpp','executor_release':identity} or settings.executor_release!=identity:
        raise AppError('native_release_invalid','执行器源码身份未通过核对，请管理员检查安装；未切换发送方式',503)
    value=json.loads((Path(settings.data_dir)/'native-selection.json').read_text())
    build=read_build(settings.root)
    if value['sources']!=sources or value['binary_sha256']!=build.binary_sha256:
        raise AppError('native_release_invalid','执行器编译身份未通过核对，请检查安装',503)
    return build


def select_native(root,data_dir):
    identity,sources=release_identity(root);build=read_build(root)
    folder=private_directory(data_dir);path=folder/'native-selection.json'
    value={'schema':1,'executor':'cpp','release_sha256':identity,
           'sources':sources,'binary_sha256':build.binary_sha256}
    if path.exists() or path.is_symlink():
        if selection_values(folder)=={'executor':'cpp','executor_release':identity} and json.loads(path.read_text())==value:
            return value
        raise AppError('native_selection_conflict','已有执行器选择不同，未覆盖；请先完成受控切换',409)
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as stream:
        json.dump(value,stream,sort_keys=True,indent=2);stream.write('\n');stream.flush();os.fsync(stream.fileno())
    parent=os.open(folder,os.O_RDONLY)
    try:os.fsync(parent)
    finally:os.close(parent)
    return value
