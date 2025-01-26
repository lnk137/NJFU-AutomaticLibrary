from utils.base_system import BaseSystem
from utils.password_encryptor import PasswordEncryptor
from datetime import datetime, timedelta

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
        return {
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
        if result.get('code') != 0:
            return result.get('message')

        return {
            "message": result["message"],
            "resvName": result["data"]["resvName"],
            "roomName": result["data"]["resvDevInfoList"][0]["roomName"],
            "devName": result["data"]["resvDevInfoList"][0]["devName"],
        }

    def reserve_seat(self, seat_list, begin_time="10:30", end_time="22:00"):
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

            # 设置预约时间
            resv_begin_time, resv_end_time = self.get_reservation_time(begin_time, end_time)

            fail_message = []
            # 遍历座位列表，尝试预约
            for seat_id in seat_list:
                log(f"\n尝试预约座位: {seat_id}")
                result = self.reserve_single_seat(user_info, seat_id, resv_begin_time, resv_end_time)

                # 如果是成功的预约结果，返回成功信息
                if isinstance(result, dict):
                    log(f"座位 {seat_id} 预约成功!")
                    result_data = f"{result['message']}，{result['resvName']}，{result['roomName']}，{result['devName']}"
                    log("预约信息:", result_data)
                    return result_data, user_info, fail_message

                # 如果失败，记录失败信息
                log(f"座位 {seat_id} 预约失败: {result}")
                if result not in fail_message:
                    fail_message.append(result)

            # 如果所有座位都预约失败
            return "无已预约结果", user_info, fail_message

        except Exception as e:
            log(f"预约过程出现异常: {str(e)}")
            return "无已预约结果", "无用户信息", [f"出现异常: {str(e)}"]