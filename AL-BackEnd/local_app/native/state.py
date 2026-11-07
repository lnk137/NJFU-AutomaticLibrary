"""An independent anchor distinguishes first installation from lost state."""
import json
import os
from pathlib import Path
import re
import sqlite3
import stat

from ..models import AppError


def has_native_records(database):
    return database.campus_runs.find_one({'$or':[{'native_binding':{'$exists':True}},
        {'execution_source':'native_campus_first'}]},{'_id':1}) is not None


def prepare_state(data_dir,database):
    data=Path(data_dir);marker=data/'native-state.json';directory=data/'native'
    if not marker.exists() and not marker.is_symlink():
        if directory.exists() or directory.is_symlink() or has_native_records(database):
            raise AppError('native_state_invalid','执行记录来源未确认，未创建新账本；请管理员核对原记录',503)
        return True,None
    try:
        info=marker.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077
                or info.st_nlink!=1 or info.st_size>1024):raise ValueError()
        value=json.loads(marker.read_text())
        if set(value)!={'schema','identity'} or value['schema']!=1 or not re.fullmatch('[0-9a-f]{64}',value['identity']):raise ValueError()
        for relative in ('ledger/identity','ledger/state.db','outbox/management.db'):
            path=directory/relative
            if path.is_symlink() or not path.is_file():raise ValueError()
        # Validate old epochs before starting a process can increment them.
        # Repeated restart cannot erase evidence of a restored/lost ledger.
        metadata=[]
        for relative in ('ledger/state.db','outbox/management.db'):
            db=sqlite3.connect((directory/relative).as_uri()+'?mode=ro',uri=True,timeout=2)
            try:metadata.append(dict(db.execute("SELECT key,value FROM meta WHERE key IN ('identity','epoch')")))
            finally:db.close()
        if (any(row.get('identity')!=value['identity'] for row in metadata)
                or (directory/'ledger/identity').read_text()!=value['identity']
                or not 0<int(metadata[1]['epoch'])<=int(metadata[0]['epoch'])):raise ValueError()
        return False,value['identity']
    except (OSError,ValueError,TypeError,KeyError,sqlite3.Error):
        raise AppError('native_state_invalid','持久执行记录缺失或身份不符，保持暂停；未重建账本',503) from None


def anchor_state(data_dir,identity):
    path=Path(data_dir)/'native-state.json'
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as stream:
        json.dump({'schema':1,'identity':identity},stream);stream.write('\n');stream.flush();os.fsync(stream.fileno())
    parent=os.open(path.parent,os.O_RDONLY)
    try:os.fsync(parent)
    finally:os.close(parent)
