"""Inspect legacy scripts without importing them or their network-capable modules."""
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ('AL-BackEnd/test/删除测试.py', 'AL-BackEnd/test/查询预约测试.py')
ENV_NAMES = {'NJFU_LEGACY_STUDENT_ID', 'NJFU_LEGACY_LIBRARY_PASSWORD',
             'NJFU_LEGACY_VPN_PASSWORD', 'NJFU_LEGACY_CANCEL_RECORD_ID'}


class LegacyCredentialHygieneTests(unittest.TestCase):
    def test_account_configuration_contains_no_embedded_values(self):
        for name in SCRIPTS:
            with self.subTest(path=name):
                tree = ast.parse((ROOT / name).read_text())
                embedded = []
                for node in ast.walk(tree):
                    if isinstance(node, ast.Dict):
                        for key, value in zip(node.keys, node.values):
                            if (isinstance(key, ast.Constant) and key.value in
                                    ('pid', 'lib_password', 'vpn_password', 'owned_seat', 'uuid', 'record_id')
                                    and isinstance(value, ast.Constant) and value.value
                                    and value.value not in ENV_NAMES):
                                embedded.append(value.lineno)
                    if (isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'uuid' for t in node.targets)
                            and isinstance(node.value, ast.Constant) and node.value.value):
                        embedded.append(node.lineno)
                self.assertFalse(embedded, 'Embedded legacy credential or record at lines: ' + str(embedded))

    def test_missing_environment_is_rejected_before_any_legacy_dependency_is_loaded(self):
        for name in SCRIPTS:
            with self.subTest(path=name):
                tree = ast.parse((ROOT / name).read_text())
                loader = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'load_test_config'), None)
                self.assertIsNotNone(loader, 'Legacy configuration must come from an explicit environment loader')
                # Execute only this isolated loader AST; never import the script.
                namespace = {'os': SimpleNamespace(environ={})}
                exec(compile(ast.Module(body=[loader], type_ignores=[]), name, 'exec'), namespace)
                with self.assertRaisesRegex(RuntimeError, 'environment'):
                    namespace['load_test_config']()
                values = {'NJFU_LEGACY_STUDENT_ID': 'fixture-only',
                          'NJFU_LEGACY_LIBRARY_PASSWORD': 'fixture-library',
                          'NJFU_LEGACY_VPN_PASSWORD': 'fixture-vpn',
                          'NJFU_LEGACY_CANCEL_RECORD_ID': 'fixture-record'}
                namespace['os'].environ = values
                result = namespace['load_test_config']()
                self.assertEqual(result['pid'], 'fixture-only')
                self.assertEqual(result['lib_password'], 'fixture-library')
                self.assertEqual(result['vpn_password'], 'fixture-vpn')
                expected = {'pid', 'lib_password', 'vpn_password'}
                if '删除' in name:
                    expected.add('record_id')
                    self.assertEqual(result['record_id'], 'fixture-record')
                self.assertEqual(set(result), expected)
                for field in expected:
                    variable = {'pid': 'NJFU_LEGACY_STUDENT_ID', 'lib_password': 'NJFU_LEGACY_LIBRARY_PASSWORD',
                                'vpn_password': 'NJFU_LEGACY_VPN_PASSWORD', 'record_id': 'NJFU_LEGACY_CANCEL_RECORD_ID'}[field]
                    namespace['os'].environ = {key: value for key, value in values.items() if key != variable}
                    with self.assertRaisesRegex(RuntimeError, 'environment'):
                        namespace['load_test_config']()


if __name__ == '__main__':
    unittest.main()
