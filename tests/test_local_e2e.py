import sys
import tempfile
import threading
import unittest
from pathlib import Path
import requests
from werkzeug.serving import make_server
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'AL-BackEnd'))
from support.mongo_fixture import MongoFixture
from support.account_fixture import School
from support.auth_fixture import grant, FIXTURE_PASSWORD


class EndToEndTests(unittest.TestCase):
    def test_two_accounts_cross_real_http_and_survive_switch(self):
        from local_app.runtime import create_runtime
        from local_app.config import load_settings
        with tempfile.TemporaryDirectory() as d:
            mongo=MongoFixture();runtime=None;server=None
            try:
                import socket
                with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
                settings=load_settings(Path(__file__).resolve().parents[1],{'data_dir':Path(d),'mongo_uri':mongo.uri,'database_name':mongo.database().name,'port':port})
                runtime=create_runtime(settings,client_factory=School)
                runtime.auth.initialize_root(FIXTURE_PASSWORD,['2310203000'])
                grant(runtime.auth,['fixture01','fixture02']);runtime.start()
                server=make_server('127.0.0.1',port,runtime.app,threaded=True)
                runner=threading.Thread(target=server.serve_forever);runner.start()
                base=f'http://127.0.0.1:{port}';http=requests.Session();http.trust_env=False
                token=http.get(base+'/api/local/bootstrap').json()['csrf']
                headers={'Origin':base,'X-CSRF-Token':token}
                self.assertEqual(http.post(base+'/api/local/auth/login',headers=headers,json={'kind':'root','username':'root','password':FIXTURE_PASSWORD}).status_code,200)
                ids=[]
                for n in (1,2):
                    r=http.post(base+'/api/local/accounts/verify',headers=headers,json={'student_id':f'fixture0{n}','password':'pass','alias':f'同学{n}'})
                    self.assertEqual(r.status_code,200);ids.append(r.json()['account']['id'])
                for n,id in enumerate(ids,1):
                    r=http.post(base+f'/api/local/accounts/{id}/query',headers=headers,json={})
                    self.assertEqual(r.json()['records'][0]['student_id'],f'fixture0{n}')
                self.assertFalse(http.get(base+'/api/local/status').json()['allow_live'])
                self.assertEqual(runtime.store.runs.count_documents({}),0)
                self.assertEqual(http.post(base+'/db/reservation/delete',headers=headers,json={}).status_code,404)
                http.close()
            finally:
                if server:server.shutdown();server.server_close();runner.join()
                if runtime:runtime.close()
                mongo.close()
