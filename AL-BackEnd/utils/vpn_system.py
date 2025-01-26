from utils.base_system import BaseSystem
from bs4 import BeautifulSoup
from utils.password_encryptor import PasswordEncryptor

def log(*args):
    """
    统一打印日志函数。

    :param args: 打印的内容
    :return: None
    """
    # print(*args)
    pass

class VPNSystem(BaseSystem):
    def __init__(self, username, password):
        super().__init__(
            username=username,
            password=password,
            base_url="https://webvpn.njfu.edu.cn/webvpn/LjIwMS4xNjkuMjE4LjE2OC4xNjc=/LjIxNC4xNTguMTk5LjEwMi4xNjIuMTU5LjIwMi4xNjguMTQ3LjE1MS4xNTYuMTczLjE0OC4xNTMuMTY1/",
            vpn_suffix=""
        )

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

    def vpn_login(self):
        """
        登录 VPN，获取有效的 VPN 会话。

        :return: 登录成功返回 True，失败返回 False
        """
        login_url = f"{self.base_url}authserver/login"
        params = {'service': 'https://webvpn.njfu.edu.cn/rump_frontend/loginFromCas/'}

        # 获取初始页面
        html_text = self.get_response(login_url, params=params).text
        if not html_text:
            return False

        # 提取表单元素
        form_elements = self.extract_form_elements(html_text)
        if not form_elements:
            return False

        salt, lt = form_elements

        # 加密密码
        encrypted_password = PasswordEncryptor.aes_encrypt_password(salt, self.password)
        if not encrypted_password:
            return False

        # 提交登录请求
        data = {
            'username': self.username,
            'password': encrypted_password,
            'lt': lt,
            'dllt': 'userNamePasswordLogin',
            'execution': 'e1s1',
            '_eventId': 'submit',
            'rmShown': '1'
        }

        response = self.session.post(login_url, data=data)

        if response and "frontend/login/index.html" in response.url:
            log("VPN登录成功")
            return True

        log("VPN登录失败")
        return False
