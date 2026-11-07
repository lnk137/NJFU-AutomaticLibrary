"""Credential state is account-scoped and trusted rejection metadata stays internal."""
import json
from pathlib import Path
import tempfile
from unittest.mock import patch
import unittest
import test_public_relay as relay
import test_local_api as api
from local_app.models import AppError
from local_app.web import create_app
from local_app.config import load_settings
from local_app.dual_node.publisher import PlanPublisherLoop


class RejectionHeaderTests(unittest.TestCase):
    setUpClass = classmethod(relay.PublicRelayTests.setUpClass.__func__)
    tearDownClass = classmethod(relay.PublicRelayTests.tearDownClass.__func__)
    setUp = relay.PublicRelayTests.setUp
    tearDown = relay.PublicRelayTests.tearDown
    public = relay.PublicRelayTests.public
    login = relay.PublicRelayTests.login

    def test_only_trusted_saved_credential_rejection_has_internal_marker(self):
        for trusted in (False, True):
            error = AppError('login_failed', 'School rejected', 401)
            if trusted: error.saved_credential_rejected = True
            with patch.object(self.auth, 'login', side_effect=error):
                response = self.login()
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.headers.get('X-NJFU-Saved-Credential-Rejected'), '1' if trusted else None)
            self.assertNotIn('saved_credential_rejected', response.json)


class CredentialProjectionTests(unittest.TestCase):
    setUpClass = classmethod(api.ApiTests.setUpClass.__func__)
    tearDownClass = classmethod(api.ApiTests.tearDownClass.__func__)
    setUp = api.ApiTests.setUp
    tearDown = api.ApiTests.tearDown

    def test_capability_reports_only_own_safe_registration_state(self):
        account = self.service.verify_and_save('fixture01', 'pass', 'A')
        class Gate:
            def is_managed(self, account_id): return True
            def set_auth_resolver(self, *args): pass
        self.service.managed_gate = Gate()
        seen = []
        def state(account_id, revision):
            seen.append((account_id, revision)); return 'maintenance_required'
        app = create_app(load_settings(Path(__file__).resolve().parents[1]), self.service,
                         self.store, self.catalog, lambda: {}, auth=self.auth, credential_sync=state)
        client = app.test_client()
        from support.auth_fixture import root_session
        root_session(self.auth, client)
        response = client.get('/api/local/accounts', base_url=self.origin)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['accounts'][0]['capabilities']['credential_sync_state'], 'maintenance_required')
        self.assertEqual(seen, [(account.id, account.credential_revision)])

    def test_publisher_state_never_claims_node_readiness(self):
        with tempfile.TemporaryDirectory() as directory:
            publisher = object.__new__(PlanPublisherLoop)
            publisher.path = Path(directory)/'publisher.json'
            config = dict(schema=1, enabled=True, ssh_config='/unused', ssh_host='unused', days=7,
                          registrations={'one': {'credential_revision': 3, 'account_ref': 'a'}})
            publisher.path.write_text(json.dumps(config)); publisher.path.chmod(0o600)
            self.assertEqual(publisher.credential_sync_state('one', 3), 'matched')
            self.assertEqual(publisher.credential_sync_state('one', 4), 'maintenance_required')
            self.assertEqual(publisher.credential_sync_state('missing', 3), 'unknown')
            publisher.path.write_text('corrupt')
            self.assertEqual(publisher.credential_sync_state('one', 3), 'unknown')
