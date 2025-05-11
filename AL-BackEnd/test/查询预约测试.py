from utils.vpn_system import VPNSystem
from utils.library_system import LibrarySystem
from datetime import datetime, timedelta
import requests
import json
from utils import config

def query_reservation(res_item):
    # 1. 加载账号、密码信息
    pid = res_item["pid"]
    vpn_password = res_item["vpn_password"]
    lib_password = res_item["lib_password"]
    
    # 如果末尾是全角"！"，就替换成半角"!"
    if lib_password.endswith('！'):
        lib_password = lib_password[:-1] + '!'

    # 同步session
    shared_session = requests.Session()
    vpn = VPNSystem(pid, vpn_password)
    library = LibrarySystem(pid, lib_password)
    vpn.session = library.session = shared_session

    # VPN登录
    if not vpn.vpn_login():
        print(f"VPN 登录失败，无法继续查询")
        return

    # 查询预约信息
    # 默认查询最近一周的预约记录
    reservations, message = library.get_reservation_info()
    
    if reservations is None:
        print(f"查询失败: {message}")
        return

    if not reservations:
        print(f"查询结果: {message}")
        return

    # 打印查询结果
    print(f"\n查询结果 ({len(reservations)} 条记录):")
    print("-" * 50)
    for resv in reservations:
        print(f"预约ID: {resv['uuid']}")
        print(f"预约人: {resv['resvName']}")
        print(f"预约时间: {resv['resvBeginTime']} - {resv['resvEndTime']}")
        print(f"座位信息: {resv['devInfo']['roomName']} - {resv['devInfo']['devName']}")
        print(f"预约状态: {resv['resvStatus']}")
        print("-" * 50)

if __name__ == "__main__":
    user_config_info = {
        "pid": "2210104120",
        "is_reserved": "True",
        "lib_password": "njfu160101!",
        "vpn_password": "AAa040328/",
        "updated_at": "2025-05-10 23:54:59"
    }
    query_reservation(user_config_info)
