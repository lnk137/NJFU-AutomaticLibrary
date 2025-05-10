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
# 日志配置
log_path = os.path.dirname(config.LOG_FILE)
if not os.path.exists(log_path):
    os.makedirs(log_path)
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [%(name)s] - %(message)s",
    handlers=[
        logging.FileHandler(config.LOG_FILE, encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)
log = logger.info

# MongoDB 初始化
mongo_client = MongoClient(f"mongodb://{config.DB_IP}/")
db = mongo_client.AutoLib
user_config_info = db.user_config_info  # 存储预约记录
users_col = db.users  # 存储用户信息

# 数据库操作函数

def get_all_active_reservations():
    """获取所有正在预约的记录"""
    return list(user_config_info.find({"is_reserved": "True"}).sort("priority", DESCENDING))


def insert_user(user_info):
    """插入用户信息到 users 集合"""
    users_col.update_one(
        {"pid": user_info.get("pid")},
        {"$set": user_info},
        upsert=True
    )


def insert_or_update_reservation_result(pid,res_message):
    """更新 reservations 集合中的预约结果"""
    user_config_info.update_one(
        {"pid": pid},
        {"$set": {"result": res_message, "updated_at": datetime.now().strftime('%Y-%m-%d %H:%M:%S')}},
        upsert=False
    )

# 座位ID 转换

def get_seat_ids(seat_list):
    """根据设备名称列表获取设备 ID"""
    seat_ids = []
    for device_name in seat_list:
        device = db.devices.find_one({"devName": device_name}, {"_id": 0, "devId": 1})
        if device:
            seat_ids.append(device["devId"])
        else:
            log(f"设备号 {device_name} 不存在")
    return seat_ids

# 预约逻辑

def reservation(res_item):
    #1.加载账号、密码、座位信息
    pid = res_item["pid"]
    vpn_password = res_item["vpn_password"]
    lib_password = res_item["lib_password"]
    # 如果末尾是全角“！”，就替换成半角“!”
    if lib_password.endswith('！'):
        lib_password = lib_password[:-1] + '!'
    seat_list = res_item["seat_list"]
    #2.根据选择模式计算日期
    resv_begin_time=""
    resv_end_time=""

    mode = res_item["mode"]
    if mode == "week":
        tomorrow = datetime.now() + timedelta(days=1)
        weekday_iso = str(tomorrow.isoweekday())  # 1~7
        print(f"{res_item['time']['week_time'][weekday_iso]}")
        begin_time, end_time = res_item['time']['week_time'][weekday_iso].split("-")
        resv_begin_time = tomorrow.strftime("%Y-%m-%d") + f" {begin_time}:00"
        resv_end_time = tomorrow.strftime("%Y-%m-%d") + f" {end_time}:00"

        print(f"{resv_begin_time=}-{resv_end_time=}")
    elif mode == "tomorrow":
        res_time=res_item["time"]["tomorrow"]
        begin_time, end_time = res_time.split("-")
        today = datetime.now().date()
        resv_begin_time = today.strftime("%Y-%m-%d") + f" {begin_time}:00"
        resv_end_time = today.strftime("%Y-%m-%d") + f" {end_time}:00"
        print(f"{resv_begin_time=}-{resv_end_time=}")

    elif mode == "after_tomorrow":
        res_time=res_item["time"]["tomorrow"]
        begin_time, end_time = res_time.split("-")
        tomorrow = datetime.now() + timedelta(days=1)
        resv_begin_time = tomorrow.strftime("%Y-%m-%d") + f" {begin_time}:00"
        resv_end_time = tomorrow.strftime("%Y-%m-%d") + f" {end_time}:00"
        print(f"{resv_begin_time=}-{resv_end_time=}")

    #将计算好的日期保存到此用户
    #3.计算座位号
    seat_ids = get_seat_ids(seat_list)

    #4.同步session
    shared_session = requests.Session()
    vpn = VPNSystem(pid, vpn_password)
    library = LibrarySystem(pid, lib_password)
    vpn.session = library.session = shared_session

    # shared_session = requests.Session()
    # vpn = VPNSystem(pid, vpn_password)
    # vpn.session = shared_session
    # library = LibrarySystem(pid, lib_password)
    # library.session = shared_session


#-----------------------------------------------------------
    if not vpn.vpn_login():
        log(f"VPN 登录失败，无法继续预约")
        return

    try:

        res_message ,user_info= library.reserve_seat(
            seat_list=seat_ids,
            resv_begin_time=resv_begin_time,
            resv_end_time=resv_end_time
        )
        print(f"{res_message=}")

        insert_user(user_info)
        insert_or_update_reservation_result(user_info["pid"], res_message)
    except Exception as e:
        insert_or_update_reservation_result(pid, "登录失败，请自行验证账号密码")
        print(f"{e=}")

# 执行所有预约

def process_reservations():
    active_list = get_all_active_reservations()
    if not active_list:
        log("没有正在预约中的记录")
        return
    log("开始处理预约列表，共 {} 条".format(len(active_list)))
    for item in active_list:
        log(f"预约学号: {item['pid']}, 优先级: {item['priority']}")
        print(f"{item=}")
        reservation(item)
    log("预约处理结束")

if __name__ == "__main__":
    process_reservations()
