import base64
import random
import string
from datetime import datetime, timedelta
import requests
from Crypto.Cipher import AES
from Crypto.Cipher import PKCS1_v1_5
from Crypto.PublicKey import RSA
from Crypto.Util.Padding import pad
from bs4 import BeautifulSoup
from utils import config

def log(*args):
    """
    统一打印日志函数。

    :param args: 打印的内容
    :return: None
    """
    # print(*args)
    pass


def set_public_key(public_key):
    """
    将公钥字符串格式化为 PEM 格式，并导入为 RSA 公钥。

    :param public_key: 公钥字符串
    :return: RSA 公钥对象
    """
    pem = "-----BEGIN PUBLIC KEY-----\n"
    pem += "\n".join(public_key[i:i + 64] for i in range(0, len(public_key), 64))
    pem += "\n-----END PUBLIC KEY-----"
    return RSA.importKey(pem)


def encrypt_with_public_key(public_key, text):
    """
    使用 RSA 公钥加密文本。

    :param public_key: RSA 公钥对象
    :param text: 需要加密的文本
    :return: 加密后的文本（base64 编码字符串）
    """
    text_bytes = text.encode('utf-8')
    cipher = PKCS1_v1_5.new(public_key)
    encrypted = cipher.encrypt(text_bytes)
    return base64.b64encode(encrypted).decode('utf-8')


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

    def vpn_login(self):
        """
        登录 VPN，获取有效的 VPN 会话。

        :return: 登录成功返回 True，失败返回 False
        """
        login_url = "https://webvpn.njfu.edu.cn/webvpn/LjIwMS4xNjkuMjE4LjE2OC4xNjc=/LjIxNC4xNTguMTk5LjEwMi4xNjIuMTU5LjIwMi4xNjguMTQ3LjE1MS4xNTYuMTczLjE0OC4xNTMuMTY1/authserver/login"
        params = {'service': 'https://webvpn.njfu.edu.cn/rump_frontend/loginFromCas/'}

        # 获取初始页面
        init_resp = self.session.get(login_url, params=params)
        log("VPN初始页面响应状态码:", init_resp.status_code)

        if init_resp.status_code != 200:
            log("VPN初始页面获取失败")
            return False

        soup = BeautifulSoup(init_resp.text, 'html.parser')
        salt_input = soup.find('input', {'id': 'pwdDefaultEncryptSalt'})
        lt_input = soup.find('input', {'name': 'lt'})

        if not salt_input or not lt_input:
            log("未找到必要的表单元素")
            return False

        # 加密密码
        random_prefix = ''.join(random.choice(string.ascii_letters + string.digits) for _ in range(64))
        random_iv = ''.join(random.choice(string.ascii_letters + string.digits) for _ in range(16))
        cipher = AES.new(salt_input['value'].encode('utf-8'), AES.MODE_CBC, random_iv.encode('utf-8'))
        encrypted_password = base64.b64encode(
            cipher.encrypt(pad((random_prefix + config.VPN_PASSWORD).encode('utf-8'), AES.block_size))).decode('utf-8')

        # 登录请求
        data = {
            'username': config.VPN_USERNAME,
            'password': encrypted_password,
            'lt': lt_input['value'],
            'dllt': 'userNamePasswordLogin',
            'execution': 'e1s1',
            '_eventId': 'submit',
            'rmShown': '1'
        }
        response = self.session.post(login_url, params=params, data=data, allow_redirects=True)
        log("VPN登录响应状态码:", response.status_code)

        if "frontend/login/index.html" not in response.url:
            log("VPN登录失败")
            return False

        log("VPN登录成功")
        return True

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
        public_key = set_public_key(key_data['data']['publicKey'])
        encrypted_password = encrypt_with_public_key(public_key, f"{self.password};{key_data['data']['nonceStr']}")

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
