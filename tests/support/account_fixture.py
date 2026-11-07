import tempfile
from pathlib import Path
from local_app.models import Identity, SchoolRecord, AppError


class School:
    """School boundary double; storage/services remain real."""
    def __init__(self, student_id, password, **kwargs):
        self.student_id,self.password=student_id,password
        self.identity=None;self.allow_live=kwargs.get('allow_live',False)
    def login(self):
        if self.password=='wrong':raise AppError('login_failed','学校登录失败',401)
        self.identity=Identity(self.student_id,'Fixture '+self.student_id,123)
        return self.identity
    def query(self,begin_date,end_date):
        return [SchoolRecord('record-'+self.student_id,self.student_id,'1',str(begin_date)+' 09:00:00',str(begin_date)+' 12:00:00',1027)]
    def close(self):pass


def services(db):
    from local_app.store import LocalStore
    from local_app.vault import CredentialVault
    from local_app.accounts import AccountService
    from local_app.models import Seat
    temp=tempfile.TemporaryDirectory(prefix='njfu-account-test-')
    store=LocalStore(db);vault=CredentialVault.open(Path(temp.name)/'key',has_encrypted_accounts=False)
    catalog={'1':Seat('1','A001','2F'),'2':Seat('2','A002','2F')}
    service=AccountService(store,vault,catalog,School)
    return temp,store,service,catalog
