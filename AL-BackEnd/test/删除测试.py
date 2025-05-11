from utils.vpn_system import VPNSystem
from utils.library_system import LibrarySystem
from datetime import datetime
from pymongo import MongoClient, ASCENDING, DESCENDING
import requests
import json
import os
import logging
from utils import config
from datetime import date, datetime, timedelta, time
# MongoDB 初始化
mongo_client = MongoClient(f"mongodb://{config.DB_IP}/")
db = mongo_client.AutoLib
user_config_info = db.user_config_info  # 存储预约记录
users_col = db.users  # 存储用户信息

def reservation(res_item):
    #1.加载账号、密码、座位信息
    pid = res_item["pid"]
    vpn_password = res_item["vpn_password"]
    lib_password = res_item["lib_password"]
    # 如果末尾是全角“！”，就替换成半角“!”
    if lib_password.endswith('！'):
        lib_password = lib_password[:-1] + '!'

    #同步session
    shared_session = requests.Session()
    vpn = VPNSystem(pid, vpn_password)
    library = LibrarySystem(pid, lib_password)
    vpn.session = library.session = shared_session


    if not vpn.vpn_login():
        print(f"VPN 登录失败，无法继续预约")
        return
    uuid="9d5daf4f4e2d446b95af0ce38f99c3f7"
    library.delete_seat(uuid)

if __name__ == "__main__":
    user_config_info={
          "pid": "2210104120",
          "is_reserved": "True",
          "lib_password": "njfu160101!",
          "mode": "week",
          "priority": 100,
          "seat_list": [
            "4F-A107",
            "5F-A002"
          ],
          "time": {
            "week_time": {
              "1": "10:00-12:00",
              "2": "10:00-12:00",
              "3": "10:00-12:00",
              "4": "10:00-12:00",
              "5": "10:00-12:00",
              "6": "10:00-12:00",
              "7": "08:00-12:00"
            },
            "tomorrow": "10:00-12:00",
            "after_tomorrow": "10:00-11:00"
          },
          "updated_at": "2025-05-10 23:54:59",
          "vpn_password": "AAa040328/",
          "result": "王琼艺 期望预约时间2025-05-11 08:00:00-12:00:00预约成功 新增成功 五层A区 5F-A002",
          "owned_seat": {
            "5F-A002": {
              "uuid": "53d81376fabe4d529448e15536aded07",
              "target_time": "2025-05-11 08:00:00-12:00:00",
              "resvStatus": "1027"
            }
          }
        }
    reservation(user_config_info)