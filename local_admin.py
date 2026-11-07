#!/usr/bin/env python3
"""Initialize the local root account once, using a non-echoing password prompt."""
import argparse
import getpass
from pathlib import Path
import sys
from pymongo import MongoClient

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT/'AL-BackEnd'))
from local_app.auth import AuthService
from local_app.config import load_settings
from local_app.models import AppError
from local_app.store import LocalStore


def main():
    parser=argparse.ArgumentParser(description='Initialize root and explicit student access once')
    parser.add_argument('--allow', action='append', required=True, help='Initial allowed student ID; repeat for each ID')
    args=parser.parse_args()
    settings=load_settings(ROOT)
    with MongoClient(settings.mongo_uri,serverSelectionTimeoutMS=2000) as mongo:
        mongo.admin.command('ping')
        auth=AuthService(LocalStore(mongo[settings.database_name]))
        if auth.security.find_one({'_id':'root'}):
            raise AppError('admin_exists','管理员已经初始化；未修改现有密码或名单',409)
        password=getpass.getpass('Root password: ')
        if getpass.getpass('Confirm password: ')!=password:
            raise AppError('password_mismatch','两次输入不一致，未创建管理员')
        auth.initialize_root(password,args.allow)
        del password
        print('管理员及指定准入名单已初始化。密码仅保存为哈希。')


if __name__=='__main__':
    try:main()
    except AppError as exc:print(str(exc),file=sys.stderr);sys.exit(1)
