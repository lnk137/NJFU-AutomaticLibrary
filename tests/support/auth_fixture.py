"""Explicit grants and sessions for isolated integration fixtures only."""
from werkzeug.security import generate_password_hash
FIXTURE_PASSWORD = 'fixture-only-admin-password'

def grant(auth, students):
    for student in students:
        auth.access.update_one({'_id':student}, {'$setOnInsert':{'enabled':True,'revision':1,'note':''}}, upsert=True)

def root_session(auth, client):
    auth.security.update_one({'_id':'root'}, {'$setOnInsert':{'password_hash':generate_password_hash(FIXTURE_PASSWORD)}}, upsert=True)
    client.set_cookie('njfu_autolib_session', auth._issue('root'), domain='127.0.0.1')
