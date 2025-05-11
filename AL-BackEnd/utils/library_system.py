from utils.base_system import BaseSystem
from utils.password_encryptor import PasswordEncryptor
from datetime import datetime, timedelta
from pymongo import MongoClient, ASCENDING, DESCENDING
from utils import config

# MongoDB 初始化
mongo_client = MongoClient(f"mongodb://{config.DB_IP}/")
db = mongo_client.AutoLib
user_config_info = db.user_config_info  # 存储预约记录
users_col = db.users  # 存储用户信息

def log(*args):
    """
    统一打印日志函数。

    :param args: 打印的内容
    :return: None
    """
    # print(*args)
    pass

class LibrarySystem(BaseSystem):
    def __init__(self, username, password):
        super().__init__(
            username=username,
            password=password,
            base_url="https://webvpn.njfu.edu.cn/webvpn/LjIwMS4xNjkuMjE4LjE2OC4xNjc=/LjIwNS4xNTguMjAwLjE3MS4xNTMuMTUwLjIxNi45Ny4yMTEuMTU2LjE1OC4xNzMuMTQ4LjE1NS4xNTUuMjE3LjEwMC4xNTAuMTY1/",
            vpn_suffix="?vpn-12-libseat.njfu.edu.cn"
        )
        self.public_key_url = f"{self.base_url}ic-web/login/publicKey{self.vpn_suffix}"
        self.login_url = f"{self.base_url}ic-web/login/user{self.vpn_suffix}"
        self.reserve_url = f"{self.base_url}ic-web/reserve{self.vpn_suffix}"


    def get_initial_cookie(self):
        """
        获取初始 Cookie 以建立会话。

        :return: 成功返回 True，失败返回 False
        """
        try:
            init_resp = self.session.get(f"{self.base_url}ic-web/default/index{self.vpn_suffix}")
            log("图书馆首页响应状态码:", init_resp.status_code)
            if init_resp.status_code != 200:
                log("图书馆首页访问失败")
                return False
            return True
        except Exception as e:
            log("获取初始 Cookie 时发生异常:", str(e))
            return False

    def get_public_key(self):
        """
        获取登录所需的公钥和随机字符串。

        :return: (public_key, nonce) 或 (None, None)
        """
        try:
            key_resp = self.session.get(self.public_key_url)
            log("获取公钥响应状态码:", key_resp.status_code)
            log("获取公钥响应内容:", key_resp.text)
            if key_resp.status_code != 200:
                log("获取公钥失败")
                return None, None

            key_data = key_resp.json()
            if key_data.get('code') != 0:
                log("公钥数据异常")
                return None, None

            public_key = key_data['data']['publicKey']
            nonce = key_data['data']['nonceStr']
            return public_key, nonce
        except Exception as e:
            log("获取公钥时发生异常:", str(e))
            return None, None

    def perform_login(self, public_key, nonce):
        """
        加密密码并发送登录请求。

        :param public_key: 公钥
        :param nonce: 随机字符串
        :return: 登录成功返回用户信息字典，失败返回 None
        """
        try:
            # 加密密码
            encrypted_password = PasswordEncryptor.encrypt_with_public_key(
                PasswordEncryptor.set_public_key(public_key),
                f"{self.password};{nonce}"
            )
            # 发送登录请求
            login_data = {
                "logonName": self.username,
                "password": encrypted_password,
                "captcha": "",
                "privacy": True,
            }
            login_resp = self.session.post(self.login_url, json=login_data)
            log("图书馆登录响应状态码:", login_resp.status_code)
            log("图书馆登录响应内容:", login_resp.text)

            if login_resp.status_code != 200:
                log("图书馆登录请求失败")
                return None

            login_result = login_resp.json()
            if login_result.get('code') != 0:
                log("图书馆登录失败:", login_result.get('message'))
                return None

            return login_result['data']
        except Exception as e:
            log("登录请求时发生异常:", str(e))
            return None

    def set_user_cookie(self, user_info):
        """
        设置用户相关的 Cookie。

        :param user_info: 用户信息字典
        """
        try:
            self.session.cookies.set(
                'ic-cookie',
                f"userid={user_info['accNo']};username={user_info['logonName']};usernumber={user_info['cardNo']};token={user_info['token']}",
                domain='njfu.edu.cn',
                path='/'
            )
            log("用户 Cookie 设置成功")
        except Exception as e:
            log("设置用户 Cookie 时发生异常:", str(e))

    def library_login(self):
        """
        登录图书馆系统，获取用户信息和登录状态。

        :return: 成功返回用户信息字典，失败返回 None
        """
        # Step 1: 获取初始 Cookie
        if not self.get_initial_cookie():
            return None

        # Step 2: 获取公钥
        public_key, nonce = self.get_public_key()
        if not public_key or not nonce:
            return None

        # Step 3: 加密密码并登录
        user_info = self.perform_login(public_key, nonce)
        if not user_info:
            return None

        # Step 4: 设置 Cookie
        self.set_user_cookie(user_info)
        log("图书馆登录成功")
        return user_info
    def get_user_info(self):
        """
        获取并返回用户信息。

        :return: 用户信息字典，失败返回 None
        """
        user_info = self.library_login()
        if not user_info:
            log("获取用户信息失败")
            return None
        user_info={
            'uuid': user_info['uuid'],
            'accNo': user_info['accNo'],
            'pid': user_info['pid'],
            'logonName': user_info['logonName'],
            'trueName': user_info['trueName'],
            'className': user_info['className'],
            'sex': user_info['sex'],
            'deptName': user_info['deptName'],
            'token': user_info['token']
        }
        return user_info

    @staticmethod
    def get_reservation_time(begin_time="10:30", end_time="22:00"):
        """
        生成预约时间。

        :param begin_time: 开始时间
        :param end_time: 结束时间
        :return: (resv_begin_time, resv_end_time)
        """
        tomorrow = datetime.now() + timedelta(days=1)
        resv_begin_time = tomorrow.strftime("%Y-%m-%d") + f" {begin_time}:00"
        resv_end_time = tomorrow.strftime("%Y-%m-%d") + f" {end_time}:00"
        return resv_begin_time, resv_end_time

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
        log(f"预约座位 {seat_id} 响应状态码: {response.status_code}")
        log(f"预约座位 {seat_id} 响应内容: {response.text}")

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

    def reserve_seat(self, seat_list, resv_begin_time, resv_end_time):
        """
        尝试为指定的座位列表进行预约。

        :param seat_list: 座位列表
        :param begin_time: 预约开始时间，默认为 "10:30"
        :param end_time: 预约结束时间，默认为 "22:00"
        :return: 预约结果信息、用户信息、失败消息列表
        """
        try:
            # 获取用户信息
            user_info = self.get_user_info()
            if not user_info:
                return "无已预约结果", "无用户信息", ["获取用户信息失败"]

            # 遍历座位列表，尝试预约
            for seat_id in seat_list:
                log(f"\n尝试预约座位: {seat_id}")
                print(f"\n尝试预约座位: {seat_id}")
                res_message = self.reserve_single_seat(user_info, seat_id, resv_begin_time, resv_end_time)
                if "预约成功" in res_message:
                    log(f"座位 {seat_id} 预约成功，停止尝试")
                    break

            return res_message,user_info

        except Exception as e:
            log(f"预约过程出现异常: {str(e)}")
            return "无已预约结果", "无用户信息", [f"出现异常: {str(e)}"]
    def delete_seat(self, uuid):
        """
        删除预约座位。

        :param uuid: 预约记录的 UUID
        :return: (bool, str) - (是否成功, 消息)
        """
        try:
            delete_url = f"{self.base_url}ic-web/reserve/cancel/{uuid}{self.vpn_suffix}"
            response = self.session.post(delete_url)
            log(f"删除座位 {uuid} 响应状态码: {response.status_code}")
            log(f"删除座位 {uuid} 响应内容: {response.text}")

            if response.status_code != 200:
                return False, f"删除座位请求失败: 状态码 {response.status_code}"

            result = response.json()
            if result.get('code') == 0:
                return True, "删除座位成功"
            else:
                return False, f"删除座位失败: {result.get('message')}"

        except Exception as e:
            error_msg = f"删除座位时发生异常: {str(e)}"
            log(error_msg)
            return False, error_msg

    def insert_or_update_mongo(self, collection_name, pid, data, upsert=True):
        """
        通用的 MongoDB 插入/更新方法。
        collection_name: 集合名（'user_config_info' 或 'users'）
        pid: 主键（如学号）
        data: 要插入或更新的数据（字典）
        upsert: 是否插入（默认True）
        返回：操作是否成功（True/False）
        """
        if collection_name == 'user_config_info':
            collection = user_config_info
        elif collection_name == 'users':
            collection = users_col
        else:
            return False
        data['updated_at'] = datetime.now()
        result = collection.update_one(
            {"pid": pid},
            {"$set": data},
            upsert=upsert
        )
        return result.modified_count > 0 or result.upserted_id

    def get_reservation_info(self, begin_date=None, end_date=None, page=1, page_num=10):
        """
        查询预约信息并写入Mongo
        """
        try:
            # 先登录图书馆系统
            user_info = self.library_login()
            if not user_info:
                # 查询不到用户的情况直接owned_seat也置空
                self.insert_or_update_mongo('user_config_info', '', {"owned_seat": {}}, upsert=True)
                return None, "图书馆登录失败，无法查询预约信息"

            if not begin_date:
                begin_date = datetime.now().strftime("%Y-%m-%d")
            if not end_date:
                end_date = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d")

            query_url = f"{self.base_url}ic-web/reserve/resvInfo{self.vpn_suffix}"
            params = {
                "beginDate": begin_date,
                "endDate": end_date,
                "needStatus": 6,  # 所有状态
                "page": page,
                "pageNum": page_num,
                "orderKey": "gmt_create",
                "orderModel": "desc"
            }
            response = self.session.get(query_url, params=params)
            result = response.json()

            if response.status_code != 200:
                # 查询失败直接owned_seat置空
                self.insert_or_update_mongo('user_config_info', user_info['pid'], {"owned_seat": {}}, upsert=True)
                return None, f"查询请求失败: 状态码 {response.status_code}"

            if result.get('code') != 0:
                self.insert_or_update_mongo('user_config_info', user_info['pid'], {"owned_seat": {}}, upsert=True)
                return None, f"查询失败: {result.get('message', '未知错误')}"

            data_list = result.get('data', [])
            formatted_data = []
            owned_seat = dict()
            if not data_list:
                # 若无记录则owned_seat置空
                self.insert_or_update_mongo('user_config_info', user_info['pid'], {"owned_seat": {}}, upsert=True)
                return [], "无预约记录"

            for item in data_list:
                begin_time = datetime.fromtimestamp(int(item.get('resvBeginTime', 0)) / 1000)
                end_time = datetime.fromtimestamp(int(item.get('resvEndTime', 0)) / 1000)
                target_time = f"{begin_time.strftime('%Y-%m-%d %H:%M:%S')}-{end_time.strftime('%H:%M:%S')}"

                dev_info_list = item.get('resvDevInfoList', [])
                dev_info = dev_info_list[0] if dev_info_list else {}
                dev_name = dev_info.get('devName', '') if dev_info_list else ''

                seat_dict = {
                    "uuid": item.get('uuid', ''),
                    "target_time": target_time,
                    "resvStatus": str(item.get('resvStatus', ''))
                }

                if dev_name:
                    if dev_name not in owned_seat:
                        owned_seat[dev_name] = []
                    owned_seat[dev_name].append(seat_dict)

                formatted_item = {
                    "uuid": item.get('uuid', ''),
                    "resvBeginTime": begin_time.strftime("%Y-%m-%d %H:%M:%S"),
                    "resvEndTime": end_time.strftime("%Y-%m-%d %H:%M:%S"),
                    "resvStatus": item.get('resvStatus', ''),
                    "resvName": item.get('resvName', ''),
                    "devInfo": dev_info,
                }
                formatted_data.append(formatted_item)
            print(f'{formatted_data=}')

            # 插入/更新到Mongo
            self.insert_or_update_mongo(
                collection_name='user_config_info',
                pid=user_info['pid'],
                data={"owned_seat": owned_seat},
                upsert=True
            )

            return formatted_data, "查询成功"

        except Exception as e:
            # 异常时同样置空
            user_pid = user_info['pid'] if ('user_info' in locals() and user_info and 'pid' in user_info) else ''
            self.insert_or_update_mongo('user_config_info', user_pid, {"owned_seat": {}}, upsert=True)
            error_msg = f"查询预约信息时发生异常: {str(e)}"
            log(error_msg)
            return None, error_msg
