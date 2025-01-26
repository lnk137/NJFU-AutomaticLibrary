
from datetime import datetime, timedelta
import requests

from bs4 import BeautifulSoup
from utils import config
from utils.password_encryptor import PasswordEncryptor
def log(*args):
    """
    统一打印日志函数。

    :param args: 打印的内容
    :return: None
    """
    # print(*args)
    pass




class LibrarySystem:
    def __init__(self, username, password):
        """
        初始化图书馆系统对象，设置 VPN 和图书馆登录所需的相关参数。

        :param username: 用户名
        :param password: 密码
        :return: None
        """
        self.session = requests.Session()
        self.username = username
        self.password = password
        self.base_url = "https://webvpn.njfu.edu.cn/webvpn/LjIwMS4xNjkuMjE4LjE2OC4xNjc=/LjIwNS4xNTguMjAwLjE3MS4xNTMuMTUwLjIxNi45Ny4yMTEuMTU2LjE1OC4xNzMuMTQ4LjE1NS4xNTUuMjE3LjEwMC4xNTAuMTY1/"
        self.vpn_suffix = "?vpn-12-libseat.njfu.edu.cn"
        self.public_key_url = f"{self.base_url}ic-web/login/publicKey{self.vpn_suffix}"
        self.login_url = f"{self.base_url}ic-web/login/user{self.vpn_suffix}"

        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
        })

    def fetch_vpn_initial_page(self, login_url, params):
        """
        获取 VPN 初始页面。

        :param login_url: VPN 登录 URL
        :param params: 请求参数
        :return: (响应文本, 响应状态码) 或 None
        """
        try:
            response = self.session.get(login_url, params=params)
            log("VPN初始页面响应状态码:", response.status_code)
            if response.status_code != 200:
                log("VPN初始页面获取失败")
                return None
            return response.text
        except Exception as e:
            log("获取 VPN 初始页面时发生异常:", str(e))
            return None

    @staticmethod
    def extract_form_elements(html_text):
        """
        从页面中提取必要的表单元素。

        :param html_text: HTML 文本
        :return: (salt, lt) 或 None
        """
        try:
            soup = BeautifulSoup(html_text, 'html.parser')
            salt_input = soup.find('input', {'id': 'pwdDefaultEncryptSalt'})
            lt_input = soup.find('input', {'name': 'lt'})
            if not salt_input or not lt_input:
                log("未找到必要的表单元素")
                return None
            return salt_input['value'], lt_input['value']
        except Exception as e:
            log("提取表单元素时发生异常:", str(e))
            return None

    def submit_vpn_login(self, login_url, params, data):
        """
        提交 VPN 登录请求。

        :param login_url: VPN 登录 URL
        :param params: 请求参数
        :param data: 表单数据
        :return: 是否登录成功
        """
        try:
            response = self.session.post(login_url, params=params, data=data, allow_redirects=True)
            log("VPN登录响应状态码:", response.status_code)
            if "frontend/login/index.html" in response.url:
                log("VPN登录成功")
                return True
            log("VPN登录失败")
            return False
        except Exception as e:
            log("提交登录请求时发生异常:", str(e))
            return False

    def vpn_login(self):
        """
        登录 VPN，获取有效的 VPN 会话。

        :return: 登录成功返回 True，失败返回 False
        """
        login_url = "https://webvpn.njfu.edu.cn/webvpn/LjIwMS4xNjkuMjE4LjE2OC4xNjc=/LjIxNC4xNTguMTk5LjEwMi4xNjIuMTU5LjIwMi4xNjguMTQ3LjE1MS4xNTYuMTczLjE0OC4xNTMuMTY1/authserver/login"
        params = {'service': 'https://webvpn.njfu.edu.cn/rump_frontend/loginFromCas/'}

        # 获取初始页面
        html_text = self.fetch_vpn_initial_page(login_url, params)
        if not html_text:
            return False

        # 提取表单元素
        form_elements = self.extract_form_elements(html_text)
        if not form_elements:
            return False

        salt, lt = form_elements

        # 加密密码
        encrypted_password = PasswordEncryptor.aes_encrypt_password(salt, config.VPN_PASSWORD)
        if not encrypted_password:
            return False

        # 提交登录请求
        data = {
            'username': config.VPN_USERNAME,
            'password': encrypted_password,
            'lt': lt,
            'dllt': 'userNamePasswordLogin',
            'execution': 'e1s1',
            '_eventId': 'submit',
            'rmShown': '1'
        }
        return self.submit_vpn_login(login_url, params, data)

    def library_login(self):
        """
        登录图书馆系统，获取用户信息和登录状态。

        :return: 成功返回用户信息字典，失败返回 None
        """
        # 访问首页获取cookie
        init_resp = self.session.get(f"{self.base_url}ic-web/default/index{self.vpn_suffix}")
        log("图书馆首页响应状态码:", init_resp.status_code)

        if init_resp.status_code != 200:
            log("图书馆首页访问失败")
            return None

        # 获取公钥
        key_resp = self.session.get(self.public_key_url)
        log("获取公钥响应状态码:", key_resp.status_code)
        log("获取公钥响应内容:", key_resp.text)

        if key_resp.status_code != 200:
            log("获取公钥失败")
            return None

        key_data = key_resp.json()
        if key_data.get('code') != 0:
            log("公钥数据异常")
            return None

        # 加密密码
        public_key = PasswordEncryptor.set_public_key(key_data['data']['publicKey'])
        encrypted_password = PasswordEncryptor.encrypt_with_public_key(public_key, f"{self.password};{key_data['data']['nonceStr']}")

        # 登录请求
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

        user_info = login_result['data']
        # 设置cookie
        self.session.cookies.set(
            'ic-cookie',
            f"userid={user_info['accNo']};username={user_info['logonName']};usernumber={user_info['cardNo']};token={user_info['token']}",
            domain='njfu.edu.cn',
            path='/'
        )
        log("图书馆登录成功")
        return user_info

    def reserve_seat(self, seat_list, begin_time="10:30", end_time="22:00"):
        """
        尝试为指定的座位列表进行预约。

        :param seat_list: 座位列表
        :param begin_time: 预约开始时间，默认为 "10:30"
        :param end_time: 预约结束时间，默认为 "22:00"
        :return: 预约结果信息、用户信息、失败消息列表
        """
        try:
            result_data = None  # 初始化 result_data
            filtered_data = None  # 初始化 filtered_data
            fail_message = []  # 初始化 fail_message

            # 获取用户信息
            user_info = self.library_login()
            if not user_info:
                log("获取用户信息失败")
                return None, None, ["获取用户信息失败"]

            # 提取用户信息
            filtered_data = {
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

            # 设置预约时间
            tomorrow = datetime.now() + timedelta(days=1)
            resv_begin_time = tomorrow.strftime("%Y-%m-%d") + f" {begin_time}:00"
            resv_end_time = tomorrow.strftime("%Y-%m-%d") + f" {end_time}:00"

            # 预约座位请求地址
            reserve_url = f"{self.base_url}ic-web/reserve{self.vpn_suffix}"
            self.session.headers.update({
                "Content-Type": "application/json;charset=UTF-8",
                "token": user_info['token']
            })

            # 遍历座位列表，尝试预约
            for seat_id in seat_list:
                log(f"\n尝试预约座位: {seat_id}")
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

                # 发送预约请求
                response = self.session.post(reserve_url, json=resv_data)
                log(f"预约响应状态码: {response.status_code}")
                log(f"预约响应内容: {response.text}")

                if response.status_code != 200:
                    log(f"座位 {seat_id} 预约请求失败")
                    fail_message.append(f"座位 {seat_id} 请求失败: 状态码 {response.status_code}")
                    continue

                result = response.json()
                if result.get('code') != 0:
                    log(f"座位 {seat_id} 预约失败: {result.get('message')}")
                    if result.get("message") not in fail_message:
                        fail_message.append(result.get('message'))
                    continue

                # 预约成功
                log(f"座位 {seat_id} 预约成功!")
                result_data = {
                    "message": result["message"],
                    "resvName": result["data"]["resvName"],
                    "roomName": result["data"]["resvDevInfoList"][0]["roomName"],
                    "devName": result["data"]["resvDevInfoList"][0]["devName"],
                }
                result_data = f"{result_data['message']}，{result_data['resvName']}，{result_data['roomName']}，{result_data['devName']}"
                log("预约信息:", result_data)

                # 如果预约成功，则直接返回
                return result_data, filtered_data, fail_message

        except Exception as e:
            log(f"预约过程出现异常: {str(e)}")
        finally:
            # 返回结果之前，将 None 转换为字符串
            result_data = result_data if result_data is not None else "无已预约结果"
            filtered_data = filtered_data if filtered_data is not None else "无用户信息"
            fail_message = fail_message if fail_message else ["无失败信息"]
            # 转换为字符串
            fail_message = ", ".join(map(str, fail_message))

            # 返回结果
            return result_data, filtered_data, fail_message
