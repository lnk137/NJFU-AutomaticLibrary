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

            owned_seat= {dev_id: {}}
            owned_seat[dev_id]["uuid"]=f"{uuid}"
            owned_seat[dev_id]["target_time"] = f"{target_time}"
            owned_seat[dev_id]["resvStatus"] = f"{resvStatus}"
            self.insert_owned_seat(user_info['pid'], owned_seat)
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
        删除已预约的座位。

        :param uuid: 预约记录的 UUID
        :return: (bool, str) - (是否成功, 消息)
        """
        try:
            # 先登录图书馆系统
            user_info = self.library_login()
            if not user_info:
                return False, "图书馆登录失败，无法删除座位"

            delete_url = f"{self.base_url}ic-web/reserve/delete{self.vpn_suffix}"
            
            # 构建请求数据
            payload = {
                "uuid": uuid
            }

            # 发起删除请求
            response = self.session.post(delete_url, json=payload)
            result = response.json()
            print(result.get("message"))
            user_record = user_config_info.find_one({"owned_seat": {"$exists": True}})
            if user_record and "owned_seat" in user_record:
                # 遍历所有座位，找到匹配的 UUID 并删除
                for dev_id, seat_info in user_record["owned_seat"].items():
                    if seat_info.get("uuid") == uuid:
                        # 使用 $unset 操作符删除匹配的座位信息
                        user_config_info.update_one(
                            {"pid": user_record["pid"]},
                            {"$unset": {f"owned_seat.{dev_id}": ""}}
                        )
                        log(f"成功从数据库删除座位记录: {dev_id}")
                        break
            return result.get("message")

        except Exception as e:
            error_msg = f"删除座位时发生异常: {str(e)}"
            log(error_msg)
            return False, error_msg

    def insert_owned_seat(self, pid, owned_seat):
        """
        将预约成功的座位信息插入到数据库。

        :param pid: 用户学号
        :param owned_seat: 预约成功的座位信息字典
        :return: 成功返回 True，失败返回 False
        """
        try:
            # 更新用户配置信息中的已预约座位
            result = user_config_info.update_one(
                {"pid": pid},
                {"$set": {"owned_seat": owned_seat, "updated_at": datetime.now()}},
                upsert=False
            )
            
            if result.modified_count > 0:
                log(f"成功更新用户 {pid} 的预约座位信息")
                return True
            else:
                log(f"未找到用户 {pid} 的配置信息，无法更新预约座位")
                return False
                
        except Exception as e:
            log(f"插入预约座位信息时发生异常: {str(e)}")
            return False

    def get_reservation_info(self, begin_date=None, end_date=None, page=1, page_num=10):
        """
        查询预约信息。

        :param begin_date: 开始日期，格式：YYYY-MM-DD
        :param end_date: 结束日期，格式：YYYY-MM-DD
        :param page: 页码
        :param page_num: 每页数量
        :return: 预约信息列表
        """
        try:
            # 先登录图书馆系统
            user_info = self.library_login()
            if not user_info:
                return None, "图书馆登录失败，无法查询预约信息"

            # 如果没有指定查询开始日期，就取"今天"
            if not begin_date:
                begin_date = datetime.now().strftime("%Y-%m-%d")

            # 如果没有指定查询结束日期，就取"今天"往后推 3 天
            if not end_date:
                end_date = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d")

            # 构建查询 URL
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

            # 发起查询请求
            response = self.session.get(query_url, params=params)
            result = response.json()
            
            if response.status_code != 200:
                return None, f"查询请求失败: 状态码 {response.status_code}"

            if result.get('code') != 0:
                return None, f"查询失败: {result.get('message', '未知错误')}"

            # 检查是否有数据
            data_list = result.get('data', [])
            if not data_list:
                return [], "无预约记录"

            # 整理返回数据
            formatted_data = []
            for item in data_list:
                # 转换时间戳为可读格式
                begin_time = datetime.fromtimestamp(int(item.get('resvBeginTime', 0)) / 1000)
                end_time = datetime.fromtimestamp(int(item.get('resvEndTime', 0)) / 1000)
                
                formatted_item = {
                    "uuid": item.get('uuid', ''),
                    "resvBeginTime": begin_time.strftime("%Y-%m-%d %H:%M:%S"),
                    "resvEndTime": end_time.strftime("%Y-%m-%d %H:%M:%S"),
                    "resvStatus": item.get('resvStatus', ''),
                    "resvName": item.get('resvName', ''),
                    "devInfo": {}
                }
                
                # 处理设备信息
                dev_info_list = item.get('resvDevInfoList', [])
                if dev_info_list:
                    dev_info = dev_info_list[0]
                    formatted_item["devInfo"] = {
                        "roomName": dev_info.get('roomName', ''),
                        "devName": dev_info.get('devName', ''),
                        "devId": dev_info.get('devId', '')
                    }
                
                formatted_data.append(formatted_item)
                print(f'{formatted_data=}')

            return formatted_data, "查询成功"

        except Exception as e:
            error_msg = f"查询预约信息时发生异常: {str(e)}"
            log(error_msg)
            return None, error_msg