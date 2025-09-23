"""
图书馆座位预约系统模块

本模块实现了与图书馆座位预约系统的交互功能，包括：
1. 用户登录和认证
2. 座位预约管理
3. 预约信息查询
4. 数据库操作

主要组件：
- LibrarySystem: 核心类，处理所有与图书馆系统的交互
- 数据库操作：使用 MongoDB 存储用户信息和预约记录
- 日志系统：记录操作日志和错误信息

注意事项：
- 所有网络请求都需要通过 VPN
- 密码使用 RSA 公钥加密
- 预约操作需要先登录
- 所有操作都有详细的日志记录
"""

from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any, Union

import requests
from pymongo import MongoClient, ASCENDING, DESCENDING
import time
import logging

from utils.base_system import BaseSystem
from utils.password_encryptor import PasswordEncryptor
from utils import config
from utils.vpn_system import VPNSystem

# 获取日志记录器
logger = logging.getLogger(__name__)

def log_with_user(level: str, user: str, operation: str, message: str) -> None:
    """
    统一的日志记录函数
    
    Args:
        level: 日志级别
        user: 用户标识
        operation: 操作类型
        message: 日志消息
    """
    extra = {'user': user, 'operation': operation}
    if level == 'info':
        logger.info(message, extra=extra)
    elif level == 'error':
        logger.error(message, extra=extra)
    elif level == 'warning':
        logger.warning(message, extra=extra)
    elif level == 'debug':
        logger.debug(message, extra=extra)

# MongoDB 初始化
mongo_client = MongoClient(f"mongodb://{config.DB_IP}/")
db = mongo_client.AutoLib
user_config_info = db.user_config_info  # 存储用户配置和预约记录
users_col = db.users  # 存储用户基本信息
devices_col = db.devices  # 存储设备信息

class LibrarySystem(BaseSystem):
    """
    图书馆座位预约系统类

    处理所有与图书馆座位预约系统相关的操作，包括登录、预约、查询等。
    继承自 BaseSystem，使用共享的会话管理。

    Attributes:
        base_url (str): 图书馆系统基础URL
        vpn_suffix (str): VPN访问后缀
        user_info (Optional[Dict]): 用户信息
        session (requests.Session): HTTP会话对象
        vpn (Optional[VPNSystem]): VPN系统实例
    """

    # 系统URL配置
    BASE_URL = "https://webvpn.njfu.edu.cn/webvpn/LjIwMS4xNjkuMjE4LjE2OC4xNjc=/LjIwNS4xNTguMjAwLjE3MS4xNTMuMTUwLjIxNi45Ny4yMTEuMTU2LjE1OC4xNzMuMTQ4LjE1NS4xNTUuMjE3LjEwMC4xNTAuMTY1/"
    VPN_SUFFIX = "?vpn-12-libseat.njfu.edu.cn"

    def __init__(
        self,
        username: str,
        password: str,
        vpn_password: Optional[str] = None,
        session: Optional[requests.Session] = None
    ) -> None:
        """
        初始化图书馆系统对象

        Args:
            username: 用户名（学号）
            password: 图书馆密码
            vpn_password: VPN密码，如果提供则自动登录VPN
            session: 可选的共享会话对象
        """
        super().__init__(
            username=username,
            password=password,
            base_url=self.BASE_URL,
            vpn_suffix=self.VPN_SUFFIX
        )

        # 系统URL
        self.public_key_url = f"{self.base_url}ic-web/login/publicKey{self.vpn_suffix}"
        self.login_url = f"{self.base_url}ic-web/login/user{self.vpn_suffix}"
        self.reserve_url = f"{self.base_url}ic-web/reserve{self.vpn_suffix}"

        # 用户信息
        self.user_info: Optional[Dict[str, Any]] = None
        self.vpn: Optional[VPNSystem] = None

        # 使用共享会话或创建新会话
        if session:
            self.session = session
        else:
            self.session = requests.Session()

        # 如果提供了VPN密码，先登录VPN
        if vpn_password:
            self._initialize_vpn(vpn_password)

        # 初始化登录
        self._initialize_login()

    def get_seat_name_by_id(self, seat_id: str) -> str:
        """
        根据座位ID获取座位名称

        Args:
            seat_id: 座位ID

        Returns:
            str: 座位名称，如果未找到则返回座位ID
        """
        try:
            device = devices_col.find_one({"devId": seat_id}, {"_id": 0, "devName": 1})
            if device:
                return device["devName"]
            else:
                return seat_id
        except Exception as e:
            log_with_user('warning', self.username, '座位名称获取', f"获取座位 {seat_id} 名称失败: {str(e)}")
            return seat_id

    def _initialize_vpn(self, vpn_password: str) -> None:
        """
        初始化并登录VPN

        Args:
            vpn_password: VPN密码

        Raises:
            Exception: VPN登录失败时抛出异常
        """
        try:
            self.vpn = VPNSystem(self.username, vpn_password)
            self.vpn.session = self.session

            if not self.vpn.vpn_login():
                raise Exception("VPN登录失败")

            # 等待VPN连接稳定（0.5秒）
            time.sleep(0.1)

        except Exception as e:
            print(f"VPN登录失败: {str(e)}")
            raise

    def _initialize_login(self) -> None:
        """
        初始化登录流程

        执行完整的登录流程：
        1. 获取初始Cookie
        2. 获取公钥
        3. 加密密码并登录
        4. 设置用户Cookie

        Raises:
            Exception: 登录过程中的任何步骤失败都会抛出异常，包含具体原因
        """
        # 获取初始Cookie
        if not self._get_initial_cookie():
            raise Exception("登录失败: 获取初始Cookie失败")

        # 获取公钥
        public_key, nonce = self._get_public_key()
        if not public_key or not nonce:
            raise Exception("登录失败: 获取公钥失败")

        # 执行登录
        user_info = self._perform_login(public_key, nonce)
        if not user_info:
            raise Exception("登录失败: 用户名或密码错误")

        # 设置Cookie和用户信息
        self._set_user_cookie(user_info)
        self.user_info = user_info

    def ensure_login(self) -> Optional[Dict[str, Any]]:
        """
        确保登录状态

        Checks the current login status and re-logs in if not logged in

        Returns:
            Optional[Dict[str, Any]]: 用户信息字典，登录失败返回None
        """
        if not self.user_info:
            self._initialize_login()
        return self.user_info

    def _get_initial_cookie(self) -> bool:
        """
        获取初始Cookie

        Returns:
            bool: 是否成功获取Cookie
        """
        try:
            init_resp = self.session.get(f"{self.base_url}ic-web/default/index{self.vpn_suffix}")
            if init_resp.status_code != 200:
                log_with_user('error', self.username, 'Cookie获取', f"获取初始Cookie失败: 状态码 {init_resp.status_code}")
                return False
            return True
        except Exception as e:
            log_with_user('error', self.username, 'Cookie获取', f"获取初始Cookie时发生异常: {str(e)}")
            return False

    def _get_public_key(self) -> Tuple[Optional[str], Optional[str]]:
        """
        获取登录所需的公钥和随机字符串

        Returns:
            Tuple[Optional[str], Optional[str]]: (公钥, 随机字符串)，获取失败返回(None, None)
        """
        try:
            key_resp = self.session.get(self.public_key_url)
            if key_resp.status_code != 200:
                log_with_user('error', self.username, '公钥获取', f"获取公钥失败: 状态码 {key_resp.status_code}")
                return None, None

            key_data = key_resp.json()
            if key_data.get('code') != 0:
                log_with_user('error', self.username, '公钥获取', f"获取公钥失败: {key_data.get('message', '未知错误')}")
                return None, None

            return key_data['data']['publicKey'], key_data['data']['nonceStr']
        except Exception as e:
            log_with_user('error', self.username, '公钥获取', f"获取公钥时发生异常: {str(e)}")
            return None, None

    def _perform_login(self, public_key: str, nonce: str) -> Optional[Dict[str, Any]]:
        """
        执行登录请求

        Args:
            public_key: RSA公钥
            nonce: 随机字符串

        Returns:
            Optional[Dict[str, Any]]: 登录成功返回用户信息，失败返回None
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

            if login_resp.status_code != 200:
                log_with_user('error', self.username, '登录', f"登录请求失败: 状态码 {login_resp.status_code}")
                return None

            login_result = login_resp.json()
            if login_result.get('code') != 0:
                log_with_user('error', self.username, '登录', f"登录失败: {login_result.get('message', '未知错误')}")
                return None

            return login_result['data']
        except Exception as e:
            log_with_user('error', self.username, '登录', f"登录请求时发生异常: {str(e)}")
            return None

    def _set_user_cookie(self, user_info: Dict[str, Any]) -> None:
        """
        设置用户Cookie

        Args:
            user_info: 用户信息字典
        """
        try:
            cookie_value = (
                f"userid={user_info['accNo']};"
                f"username={user_info['logonName']};"
                f"usernumber={user_info['cardNo']};"
                f"token={user_info['token']}"
            )
            self.session.cookies.set(
                'ic-cookie',
                cookie_value,
                domain='njfu.edu.cn',
                path='/'
            )
        except Exception as e:
            print(f"设置用户Cookie时发生异常: {str(e)}")

    def get_user_info(self) -> Optional[Dict[str, Any]]:
        """
        获取用户信息

        Returns:
            Optional[Dict[str, Any]]: 用户信息字典，包含必要的用户字段
        """
        try:
            self.ensure_login()
            if not self.user_info:
                return None

            return {
                'uuid': self.user_info['uuid'],
                'accNo': self.user_info['accNo'],
                'pid': self.user_info['pid'],
                'logonName': self.user_info['logonName'],
                'trueName': self.user_info['trueName'],
                'className': self.user_info['className'],
                'sex': self.user_info['sex'],
                'deptName': self.user_info['deptName'],
                'token': self.user_info['token']
            }
        except Exception as e:
            print(f"获取用户信息失败: {str(e)}")
            return None

    @staticmethod
    def get_reservation_time(begin_time: str = "10:30", end_time: str = "22:00") -> Tuple[str, str]:
        """
        生成预约时间

        Args:
            begin_time: 开始时间，格式 HH:MM
            end_time: 结束时间，格式 HH:MM

        Returns:
            Tuple[str, str]: (开始时间, 结束时间)，格式 YYYY-MM-DD HH:MM:SS
        """
        tomorrow = datetime.now() + timedelta(days=1)
        date_str = tomorrow.strftime("%Y-%m-%d")
        return (
            f"{date_str} {begin_time}:00",
            f"{date_str} {end_time}:00"
        )

    def _reserve_single_seat(
        self,
        user_info: Dict[str, Any],
        seat_id: str,
        resv_begin_time: str,
        resv_end_time: str
    ) -> str:
        """
        预约单个座位

        Args:
            user_info: 用户信息
            seat_id: 座位ID
            resv_begin_time: 预约开始时间
            resv_end_time: 预约结束时间

        Returns:
            str: 预约结果消息
        """
        # 获取座位名称
        seat_name = self.get_seat_name_by_id(seat_id)

        # 准备预约数据
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
        try:
            response = self.session.post(self.reserve_url, json=resv_data)
            log_with_user('info', self.username, '预约请求',
                         f"座位 {seat_name}({seat_id}) 响应状态码: {response.status_code}")

            # 记录响应内容用于调试
            log_with_user('debug', self.username, '预约响应',
                         f"座位 {seat_name}({seat_id}) 响应内容: {response.text}")

            if response.status_code != 200:
                error_msg = f"座位 {seat_name}({seat_id}) 请求失败: 状态码 {response.status_code}"
                log_with_user('error', self.username, '预约失败', error_msg)
                return error_msg

            # 处理响应结果
            result = response.json()
            target_time = f"{resv_begin_time[:10]} {resv_begin_time[11:]}-{resv_end_time[11:]}"

            if result.get('code') == 0:
                # 预约成功
                success_info = result['data']
                success_msg = (
                    f"{success_info['resvName']} 期望预约时间{target_time} "
                    f"预约成功 {result['message']} "
                    f"{success_info['resvDevInfoList'][0]['roomName']} "
                    f"{success_info['resvDevInfoList'][0]['devName']}"
                )
                log_with_user('info', self.username, '预约成功',
                             f"座位 {seat_name}({seat_id}) 预约成功: {success_msg}")
                return success_msg
            else:
                # 预约失败
                error_msg = f"座位 {seat_name}({seat_id}) 期望预约时间{target_time} 预约失败: {result['message']}"
                log_with_user('error', self.username, '预约失败', error_msg)
                return error_msg

        except requests.exceptions.RequestException as e:
            error_msg = f"座位 {seat_name}({seat_id}) 网络请求异常: {str(e)}"
            log_with_user('error', self.username, '预约异常', error_msg)
            return error_msg
        except Exception as e:
            error_msg = f"座位 {seat_name}({seat_id}) 预约过程异常: {str(e)}"
            log_with_user('error', self.username, '预约异常', error_msg)
            return error_msg

    def reserve_seat(
        self,
        seat_list: List[str],
        resv_begin_time: str,
        resv_end_time: str
    ) -> Tuple[str, Optional[Dict[str, Any]]]:
        """
        预约指定座位列表

        Args:
            seat_list: 座位ID列表
            resv_begin_time: 预约开始时间
            resv_end_time: 预约结束时间

        Returns:
            Tuple[str, Optional[Dict[str, Any]]]: (预约结果消息, 用户信息)
        """
        try:
            # 确保登录状态
            self.ensure_login()
            if not self.user_info:
                error_msg = "用户信息获取失败，无法进行预约"
                log_with_user('error', self.username, '预约', error_msg)
                return error_msg, None

            # 尝试预约每个座位
            failed_seats = []
            for seat_id in seat_list:
                seat_name = self.get_seat_name_by_id(seat_id)
                log_with_user('info', self.username, '预约', f"尝试预约座位: {seat_name}({seat_id})")

                res_message = self._reserve_single_seat(
                    self.user_info,
                    seat_id,
                    resv_begin_time,
                    resv_end_time
                )

                if "预约成功" in res_message:
                    log_with_user('info', self.username, '预约', f"座位 {seat_name}({seat_id}) 预约成功")
                    return res_message, self.user_info
                else:
                    failed_seats.append(f"{seat_name}({seat_id}): {res_message}")
                    log_with_user('warning', self.username, '预约',
                                 f"座位 {seat_name}({seat_id}) 预约失败: {res_message}")

            # 所有座位都预约失败
            if failed_seats:
                error_msg = f"所有座位预约失败，详细原因:\n" + "\n".join(failed_seats)
                log_with_user('error', self.username, '预约', error_msg)
                return error_msg, self.user_info
            else:
                error_msg = "没有可用的座位进行预约"
                log_with_user('error', self.username, '预约', error_msg)
                return error_msg, self.user_info

        except Exception as e:
            error_msg = f"预约过程出现异常: {str(e)}"
            log_with_user('error', self.username, '预约', error_msg)
            return error_msg, None

    def delete_seat(self, uuid: str) -> Tuple[bool, str]:
        """
        删除预约座位
        
        Args:
            uuid: 预约记录的UUID
            
        Returns:
            Tuple[bool, str]: (是否成功, 结果消息)
        """
        try:
            delete_url = f"{self.base_url}ic-web/reserve/delete{self.vpn_suffix}"
            response = self.session.post(
                delete_url,
                json={"uuid": uuid},
                headers={"Content-Type": "application/json;charset=UTF-8"}
            )

            print(f"删除座位 {uuid} 响应状态码: {response.status_code}")
            print(f"删除座位 {uuid} 响应内容: {response.text}")

            if response.status_code != 200:
                return False, f"删除座位请求失败: 状态码 {response.status_code}"

            result = response.json()
            if result.get('code') == 0:
                return True, "删除座位成功"
            return False, f"删除座位失败: {result.get('message')}"

        except Exception as e:
            error_msg = f"删除座位时发生异常: {str(e)}"
            print(error_msg)
            return False, error_msg

    def insert_or_update_mongo(
        self,
        collection_name: str,
        pid: str,
        data: Dict[str, Any],
        upsert: bool = True
    ) -> bool:
        """
        更新或插入MongoDB数据
        
        Args:
            collection_name: 集合名称 ('user_config_info' 或 'users')
            pid: 用户ID
            data: 要更新的数据
            upsert: 是否在记录不存在时插入
            
        Returns:
            bool: 操作是否成功
            
        Raises:
            ValueError: 当集合名称无效时抛出
        """
        # 选择集合
        collection = None
        if collection_name == 'user_config_info':
            collection = user_config_info
        elif collection_name == 'users':
            collection = users_col
        else:
            raise ValueError(f"无效的集合名称: {collection_name}")
            
        if collection is None:
            raise ValueError(f"无法获取集合: {collection_name}")
            
        try:
            # 添加更新时间
            data['updated_at'] = datetime.now()
            
            # 执行更新
            result = collection.update_one(
                {"pid": pid},
                {"$set": data},
                upsert=upsert
            )
            return bool(result.modified_count > 0 or result.upserted_id is not None)
        except Exception as e:
            print(f"MongoDB操作失败: {str(e)}")
            return False

    def get_reservation_info(
        self,
        begin_date: Optional[str] = None,
        end_date: Optional[str] = None,
        page: int = 1,
        page_num: int = 10
    ) -> Tuple[Optional[List[Dict[str, Any]]], str]:
        """
        查询预约信息
        
        Args:
            begin_date: 开始日期，默认今天
            end_date: 结束日期，默认3天后
            page: 页码
            page_num: 每页记录数
            
        Returns:
            Tuple[Optional[List[Dict[str, Any]]], str]: (预约信息列表, 结果消息)
        """
        try:
            # 确保登录状态
            self.ensure_login()
            if not self.user_info:
                return None, "图书馆登录失败，无法查询预约信息"

            # 设置查询时间范围
            if not begin_date:
                begin_date = datetime.now().strftime("%Y-%m-%d")
            if not end_date:
                end_date = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d")

            # 发送查询请求
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
            
            try:
                response = self.session.get(query_url, params=params)
                result = response.json()
            except Exception as e:
                self._clear_owned_seat()
                return None, f"查询请求失败: {str(e)}"

            # 处理查询失败
            if response.status_code != 200:
                self._clear_owned_seat()
                return None, f"查询请求失败: 状态码 {response.status_code}"

            if result.get('code') != 0:
                self._clear_owned_seat()
                return None, f"查询失败: {result.get('message', '未知错误')}"

            # 处理查询结果
            data_list = result.get('data', [])
            if not data_list:
                self._clear_owned_seat()
                return [], "无预约记录"

            try:
                # 格式化数据
                formatted_data, owned_seat = self._format_reservation_data(data_list)
                
                # 更新数据库
                if not self.insert_or_update_mongo(
                    'user_config_info',
                    self.user_info['pid'],
                    {"owned_seat": owned_seat},
                    upsert=True
                ):
                    print("更新预约信息到数据库失败")
                
                return formatted_data, "查询成功"
            except Exception as e:
                self._clear_owned_seat()
                return None, f"处理预约数据失败: {str(e)}"

        except Exception as e:
            self._clear_owned_seat()
            error_msg = f"查询预约信息时发生异常: {str(e)}"
            print(error_msg)
            return None, error_msg

    def _clear_owned_seat(self) -> None:
        """
        清空用户的座位信息
        
        在查询失败或发生异常时调用，确保用户配置被正确清空。
        """
        try:
            user_pid = (
                self.user_info['pid']
                if hasattr(self, 'user_info') and self.user_info and 'pid' in self.user_info
                else ''
            )
            if user_pid:
                self.insert_or_update_mongo(
                    'user_config_info',
                    user_pid,
                    {"owned_seat": {}},
                    upsert=True
                )
        except Exception as e:
            print(f"清空座位信息失败: {str(e)}")

    def _format_reservation_data(
        self,
        data_list: List[Dict[str, Any]]
    ) -> Tuple[List[Dict[str, Any]], Dict[str, List[Dict[str, Any]]]]:
        """
        格式化预约数据
        
        Args:
            data_list: 原始预约数据列表
            
        Returns:
            Tuple[List[Dict[str, Any]], Dict[str, List[Dict[str, Any]]]]: 
                (格式化的预约列表, 座位信息字典)
        """
        formatted_data = []
        owned_seat = {}
        
        for item in data_list:
            # 处理时间
            begin_time = datetime.fromtimestamp(int(item.get('resvBeginTime', 0)) / 1000)
            end_time = datetime.fromtimestamp(int(item.get('resvEndTime', 0)) / 1000)
            target_time = (
                f"{begin_time.strftime('%Y-%m-%d %H:%M:%S')}-"
                f"{end_time.strftime('%H:%M:%S')}"
            )

            # 处理设备信息
            dev_info_list = item.get('resvDevInfoList', [])
            dev_info = dev_info_list[0] if dev_info_list else {}
            dev_name = dev_info.get('devName', '') if dev_info_list else ''

            # 构建座位信息
            seat_dict = {
                "uuid": item.get('uuid', ''),
                "target_time": target_time,
                "resvStatus": str(item.get('resvStatus', ''))
            }

            # 更新座位字典
            if dev_name:
                if dev_name not in owned_seat:
                    owned_seat[dev_name] = []
                owned_seat[dev_name].append(seat_dict)

            # 构建格式化数据
            formatted_item = {
                "uuid": item.get('uuid', ''),
                "resvBeginTime": begin_time.strftime("%Y-%m-%d %H:%M:%S"),
                "resvEndTime": end_time.strftime("%Y-%m-%d %H:%M:%S"),
                "resvStatus": item.get('resvStatus', ''),
                "resvName": item.get('resvName', ''),
                "devInfo": dev_info,
            }
            formatted_data.append(formatted_item)

        return formatted_data, owned_seat

