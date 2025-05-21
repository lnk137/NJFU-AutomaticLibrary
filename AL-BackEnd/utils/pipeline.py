from typing import Dict, Any

from utils.base_system import BaseSystem
from utils.library_system import LibrarySystem
from utils.password_encryptor import PasswordEncryptor
from datetime import datetime, timedelta
from pymongo import MongoClient, ASCENDING, DESCENDING
from utils import config
import requests

from utils.vpn_system import VPNSystem

# MongoDB 初始化
mongo_client = MongoClient(f"mongodb://{config.DB_IP}/")
db = mongo_client.AutoLib
user_config_info = db.user_config_info  # 存储预约记录
users_col = db.users  # 存储用户信息

class Pipeline:
    def __init__(self):
        pass
    @staticmethod
    def query_reservation(res_item: Dict[str, Any]):
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

    @staticmethod
    def delete_reservation(res_item, uuid_to_delete=None):
        """
        图书馆预约及座位删除，总是完成后清理会话（session）
        :param res_item: dict，用户参数
        :param uuid_to_delete: str，可选，需要删除的座位UUID（如用于迟到保护等场景）
        """
        pid = res_item["pid"]
        vpn_password = res_item["vpn_password"]
        lib_password = res_item["lib_password"]
        # 处理末尾为全角感叹号
        if lib_password.endswith('！'):
            lib_password = lib_password[:-1] + '!'

        shared_session = requests.Session()
        try:
            vpn = VPNSystem(pid, vpn_password)
            library = LibrarySystem(pid, lib_password)
            vpn.session = library.session = shared_session

            if not vpn.vpn_login():
                print(f"[{pid}] VPN 登录失败，无法继续预约或操作")
                return
            user_info = library.library_login()
            print(f"{user_info}")
            success, msg = library.delete_seat(uuid_to_delete)
            print(f"[{pid}] delete_seat: {success}, {msg}")

        except Exception as e:
            print(f"[{pid}] 执行出现异常: {e}")
        finally:
            shared_session.close()  # 释放session确保不占用连接

    def reserve_single_seat(self, user_info, seat_id, resv_begin_time, resv_end_time):
        """
        尝试为单个座位进行预约。

        :param user_info: 用户信息字典
        :param seat_id: 座位 ID
        :param resv_begin_time: 预约开始时间
        :param resv_end_time: 预约结束时间
        :return: 成功返回预约结果字典，失败返回错误消息
        """
        resv_data = {
            "testName": "",
            "appAccNo": user_info['accNo'],
            "memberKind": 1,
            "resvDev": [seat_id],
            "resvMember": [user_info['accNo']],
            "resvProperty": 0,
            "sysKind": 8,
            "resvBeginTime": resv_begin_time,
            "resvEndTime": resv_end_time
        }

        response = self.session.post(self.reserve_url, json=resv_data)

        if response.status_code != 200:
            return f"座位 {seat_id} 请求失败: 状态码 {response.status_code}"

        result = response.json()
        target_time=f"{resv_begin_time[:10]} "+resv_begin_time[11:]+"-"+resv_end_time[11:]
        # 解析json
        if result.get('code') == 0:
            is_successd = "预约成功"
            message=f"{result['message']}"
            #成功特有字段
            uuid = result["data"]["uuid"]
            resvStatus=result["data"]["resvStatus"]
            who=f"{result['data']['resvName']}"
            where=f"{result['data']['resvDevInfoList'][0]['roomName']}"
            dev_id=f"{result['data']['resvDevInfoList'][0]['devName']}"
            res_message=f"{who} 期望预约时间{target_time} {is_successd} {message} {where} {dev_id}"

            # owned_seat= {dev_id: {}}
            # owned_seat[dev_id]["uuid"]=f"{uuid}"
            # owned_seat[dev_id]["target_time"] = f"{target_time}"
            # owned_seat[dev_id]["resvStatus"] = f"{resvStatus}"
            # self.insert_or_update_mongo('user_config_info', user_info['pid'], {"owned_seat": owned_seat})
        else:
            is_successd ="预约失败"
            message=f"{result['message']}"
            res_message=f"期望预约时间{target_time} {is_successd} {message}"

        return res_message
    # @staticmethod
    # def reserve_seat(res_item,seat_list, resv_begin_time, resv_end_time):
    #     """
    #             尝试为指定的座位列表进行预约。
    #
    #             :param seat_list: 座位列表
    #             :param begin_time: 预约开始时间，默认为 "10:30"
    #             :param end_time: 预约结束时间，默认为 "22:00"
    #             :return: 预约结果信息、用户信息、失败消息列表
    #     """
    #     try:
    #         pid = res_item["pid"]
    #         vpn_password = res_item["vpn_password"]
    #         lib_password = res_item["lib_password"]
    #         # 处理末尾为全角感叹号
    #         if lib_password.endswith('！'):
    #             lib_password = lib_password[:-1] + '!'
    #
    #         shared_session = requests.Session()
    #         try:
    #             vpn = VPNSystem(pid, vpn_password)
    #             library = LibrarySystem(pid, lib_password)
    #             vpn.session = library.session = shared_session
    #
    #             if not vpn.vpn_login():
    #                 print(f"[{pid}] VPN 登录失败，无法继续预约或操作")
    #                 return
    #             user_info = library.library_login()
    #             print(f"{user_info}")
    #
    #             # 遍历座位列表，尝试预约
    #             for seat_id in seat_list:
    #                 print(f"\n尝试预约座位: {seat_id}")
    #                 res_message = self.reserve_single_seat(self.user_info, seat_id, resv_begin_time, resv_end_time)
    #                 if "预约成功" in res_message:
    #                     log(f"座位 {seat_id} 预约成功，停止尝试")
    #                     break
    #             reservations, message = self.get_reservation_info()
    #             return res_message, self.user_info
    #
    #     except Exception as e:
    #         log(f"预约过程出现异常: {str(e)}")
    #         return "无已预约结果", "无用户信息", [f"出现异常: {str(e)}"]
if __name__ == "__main__":
    # 构造一条测试数据（请按实际账号替换）
    res_item = {
        "pid": "2210104120",
        "vpn_password": "AAa040328/",
        "lib_password": "njfu160101!",
    }
    uuid_to_delete = "34322c858660425a8adf86deb5c40561"  # 替换成你实际存在的预约uuid

    # 测试预约删除功能
    print("------ 测试预约删除功能 ------")
    Pipeline.delete_reservation(res_item, uuid_to_delete=uuid_to_delete)

    # # 如果你还想测查询功能:
    # print("------ 测试查询预约功能 ------")
    # Pipeline.query_reservation(res_item)