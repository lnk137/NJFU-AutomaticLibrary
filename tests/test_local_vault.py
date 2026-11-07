import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'AL-BackEnd'))


class VaultTests(unittest.TestCase):
    def test_authenticated_encryption_is_bound_to_account(self):
        from local_app.vault import CredentialVault
        from local_app.models import AppError
        with tempfile.TemporaryDirectory() as t:
            v=CredentialVault.open(Path(t)/'key',has_encrypted_accounts=False)
            secret=' exact-password！ '
            e=v.seal('alice',secret)
            self.assertNotIn(secret,str(e));self.assertEqual(v.unseal('alice',e),secret)
            with self.assertRaises(AppError):v.unseal('bob',e)
            e['tag']='AAAA'
            with self.assertRaises(AppError):v.unseal('alice',e)

    def test_missing_key_and_unsafe_file_are_not_replaced(self):
        from local_app.vault import CredentialVault
        from local_app.models import AppError
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'key'
            with self.assertRaises(AppError):CredentialVault.open(p,has_encrypted_accounts=True)
            self.assertFalse(p.exists())
            p.write_bytes(b'x'*32);p.chmod(0o644)
            with self.assertRaises(AppError):CredentialVault.open(p,has_encrypted_accounts=False)
            link=Path(t)/'link';link.symlink_to(p)
            with self.assertRaises(AppError):CredentialVault.open(link,has_encrypted_accounts=False)
