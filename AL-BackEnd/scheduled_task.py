"""
图书馆自动预约和迟到保护任务调度模块

本模块实现了两个主要功能：
1. 自动预约：根据用户配置自动预约图书馆座位
2. 迟到保护：对已预约的座位进行迟到保护，在用户可能迟到时自动调整预约时间

主要组件：
- 预约系统：处理用户的预约请求，包括时间计算、座位选择、预约执行等
- 迟到保护：监控已预约座位，在适当时间自动调整预约时间
- 调度系统：使用 APScheduler 管理定时任务
- 日志系统：记录所有操作和状态变化
- 数据库交互：使用 MongoDB 存储用户配置和预约信息

工作流程：
1. 自动预约：
   - 获取所有活动预约记录
   - 按优先级处理每个预约请求
   - 执行预约并更新状态
   - 记录预约结果

2. 迟到保护：
   - 扫描所有开启迟到保护的用户
   - 为每个需要保护的座位注册保护任务
   - 在指定时间执行保护动作
   - 更新预约状态

注意事项：
- 所有时间操作都基于服务器时间
- 预约时间需要提前计算
- 迟到保护在预约时间前5分钟触发
- 所有操作都有详细的日志记录
"""

import os
import time
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Any, Tuple, Optional

import requests
from pymongo import MongoClient, DESCENDING
from apscheduler.schedulers.background import BackgroundScheduler

from utils.pipeline import Pipeline
from utils.vpn_system import VPNSystem
from utils.library_system import LibrarySystem
from utils import config

# 日志配置
def setup_logging() -> logging.Logger:
    """
    配置日志系统

    设置日志格式、输出位置和日志级别。日志同时输出到文件和控制台。
    日志文件路径在 config.LOG_FILE 中配置。

    Returns:
        logging.Logger: 配置好的日志记录器
    """
    log_path = os.path.dirname(config.LOG_FILE)
    if not os.path.exists(log_path):
        os.makedirs(log_path)

    # 自定义日志格式
    log_format = (
        "[%(asctime)s] [%(levelname)s] "
        "[%(name)s] [用户:%(user)s] "
        "[%(operation)s] - %(message)s"
    )

    # 创建自定义过滤器
    class UserFilter(logging.Filter):
        def filter(self, record):
            if not hasattr(record, 'user'):
                record.user = '系统'
            if not hasattr(record, 'operation'):
                record.operation = '未知操作'
            return True

    # 创建处理器
    file_handler = logging.FileHandler(config.LOG_FILE, encoding="utf-8")
    console_handler = logging.StreamHandler()

    # 设置处理器格式
    formatter = logging.Formatter(log_format)
    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)

    # 添加过滤器
    user_filter = UserFilter()
    file_handler.addFilter(user_filter)
    console_handler.addFilter(user_filter)

    # 配置根日志记录器
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)

    # 获取当前模块的日志记录器
    logger = logging.getLogger(__name__)
    return logger

logger = setup_logging()

def log_with_user(logger, level: str, user: str, operation: str, message: str) -> None:
    """
    统一的日志记录函数

    Args:
        logger: 日志记录器
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
# 连接到MongoDB服务器，获取数据库和集合的引用
mongo_client = MongoClient(f"mongodb://{config.DB_IP}/")
db = mongo_client.AutoLib
user_config_info = db.user_config_info  # 存储用户配置和预约记录
users_col = db.users  # 存储用户基本信息

def get_all_active_reservations() -> List[Dict[str, Any]]:
    """
    获取所有正在预约的记录

    从数据库中查询所有标记为活动的预约记录，并按优先级降序排序。
    活动记录的条件是 is_reserved 字段为 "True"。

    Returns:
        List[Dict[str, Any]]: 按优先级排序的预约记录列表，每条记录包含完整的预约配置
    """
    return list(user_config_info.find({"is_reserved": "True"}).sort("priority", DESCENDING))

def get_seat_ids(seat_list: List[str]) -> List[str]:
    """
    根据设备名称列表获取设备ID

    将用户配置中的座位名称转换为系统内部的座位ID。
    如果某个座位名称在数据库中不存在，会记录警告日志但继续处理其他座位。

    Args:
        seat_list: 座位名称列表，如 ["A区-101", "B区-202"]

    Returns:
        List[str]: 座位ID列表，如 ["100500174", "100500175"]
    """
    seat_ids = []
    for device_name in seat_list:
        device = db.devices.find_one({"devName": device_name}, {"_id": 0, "devId": 1})
        if device:
            seat_ids.append(device["devId"])
        else:
            log_with_user(logger, 'warning', '系统', '座位ID获取', f"设备号 {device_name} 不存在")
    return seat_ids

def calculate_reservation_time(res_item: Dict[str, Any]) -> Tuple[str, str]:
    """
    根据预约模式计算预约时间

    支持三种预约模式：
    1. week: 根据星期几选择对应的时间段
    2. tomorrow: 预约明天的时间段
    3. after_tomorrow: 预约后天的时间段

    时间格式为 "YYYY-MM-DD HH:MM:SS"

    Args:
        res_item: 预约配置项，包含预约模式和时间设置

    Returns:
        Tuple[str, str]: (开始时间, 结束时间)

    Raises:
        ValueError: 当预约模式不支持时抛出
    """
    mode = res_item["mode"]
    now = datetime.now()

    if mode == "week_time":
        # 根据星期几选择时间
        tomorrow = now + timedelta(days=1)
        weekday_iso = str(tomorrow.isoweekday())  # 1-7 表示周一到周日
        begin_time, end_time = res_item['time']['week_time'][weekday_iso].split("-")
        date_str = tomorrow.strftime("%Y-%m-%d")
    elif mode == "tomorrow":
        # 预约明天的时间
        res_time = res_item["time"]["tomorrow"]
        begin_time, end_time = res_time.split("-")
        date_str = now.strftime("%Y-%m-%d")
    elif mode == "after_tomorrow":
        # 预约后天的时间
        res_time = res_item["time"]["tomorrow"]
        begin_time, end_time = res_time.split("-")
        tomorrow = now + timedelta(days=1)
        date_str = tomorrow.strftime("%Y-%m-%d")
    else:
        raise ValueError(f"不支持的预约模式: {mode}")

    return (
        f"{date_str} {begin_time}:00",
        f"{date_str} {end_time}:00"
    )

def update_user_config(pid: str, result: str) -> None:
    """
    更新用户配置信息

    直接更新数据库中的用户配置，记录预约结果和更新时间。
    使用 upsert 确保即使记录不存在也能创建新记录。

    Args:
        pid: 用户ID（学号）
        result: 预约结果信息
    """
    try:
        user_config_info.update_one(
            {"pid": pid},
            {
                "$set": {
                    "result": result,
                    "updated_at": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                }
            },
            upsert=True
        )
    except Exception as e:
        log_with_user(logger, 'error', pid, '用户配置更新', f"更新用户配置失败: {str(e)}")

def handle_reservation_error(pid: str, error_msg: str) -> None:
    """
    处理预约错误

    记录错误日志并更新用户配置，确保用户能看到错误信息。

    Args:
        pid: 用户ID
        error_msg: 错误信息
    """
    log_with_user(logger, 'error', pid, '预约异常', error_msg)
    update_user_config(pid, error_msg)

def reservation(res_item: Dict[str, Any]) -> None:
    """
    处理单个预约请求

    完整的预约流程：
    1. 计算预约时间
    2. 获取座位ID
    3. 登录VPN和图书馆系统
    4. 执行预约（最多重试3次）
    5. 更新用户信息
    6. 记录预约结果
    """
    # 加载账号信息
    pid = res_item["pid"]
    vpn_password = res_item["vpn_password"]
    lib_password = res_item["lib_password"].replace('！', '!')
    seat_list = res_item["seat_list"]

    try:
        # 计算预约时间
        resv_begin_time, resv_end_time = calculate_reservation_time(res_item)
        log_with_user(logger, 'info', pid, '预约时间',
                     f"预约时间: {resv_begin_time} - {resv_end_time}")

        # 获取座位ID
        seat_ids = get_seat_ids(seat_list)
        if not seat_ids:
            log_with_user(logger, 'error', pid, '座位获取', "未找到有效的座位ID")
            handle_reservation_error(pid, "未找到有效的座位ID")
            return

        # 初始化图书馆系统
        log_with_user(logger, 'info', pid, '系统初始化', "开始初始化图书馆系统")
        library = LibrarySystem(
            username=pid,
            password=lib_password,
            vpn_password=vpn_password
        )

        # 执行预约（单次尝试，不重试）
        try:
            log_with_user(logger, 'info', pid, '预约执行', "开始执行座位预约")
            res_message, user_info = library.reserve_seat(
                seat_list=seat_ids,
                resv_begin_time=resv_begin_time,
                resv_end_time=resv_end_time
            )

            # 检查预约结果
            if "成功" in res_message or "预约成功" in res_message:
                log_with_user(logger, 'info', pid, '预约结果', f"预约成功: {res_message}")

                # 更新用户信息
                if user_info:
                    library.insert_or_update_mongo(
                        collection_name="users",
                        pid=user_info.get("pid"),
                        data=user_info,
                        upsert=True
                    )
                    log_with_user(logger, 'info', pid, '用户信息', "用户信息已更新")

                # 更新预约结果
                update_user_config(pid, res_message)

                # 获取最新的预约信息
                reservations, message = library.get_reservation_info()
                if reservations:
                    log_with_user(logger, 'info', pid, '预约状态', f"当前预约状态: {message}")
                    for res in reservations:
                        log_with_user(logger, 'info', pid, '预约详情',
                                    f"座位 {res.get('devInfo', {}).get('devName', '未知')} "
                                    f"时间 {res.get('resvBeginTime')} - {res.get('resvEndTime')} "
                                    f"状态 {res.get('resvStatus')}")
                return  # 预约成功，直接返回
            else:
                error_msg = f"预约失败: {res_message}"
                log_with_user(logger, 'error', pid, '预约失败', error_msg)
                handle_reservation_error(pid, error_msg)

        except Exception as e:
            error_msg = f"预约过程发生异常: {str(e)}"
            log_with_user(logger, 'error', pid, '预约异常', error_msg)
            handle_reservation_error(pid, error_msg)

    except Exception as e:
        error_msg = f"预约过程发生异常: {str(e)}"
        log_with_user(logger, 'error', pid, '预约异常', error_msg)
        handle_reservation_error(pid, error_msg)

def process_reservations() -> None:
    """
    处理所有预约请求

    工作流程：
    1. 获取所有活动预约记录
    2. 按优先级顺序处理每个预约
    3. 记录处理结果

    每个预约都是独立处理的，一个预约的失败不会影响其他预约。
    """
    active_list = get_all_active_reservations()
    if not active_list:
        log_with_user(logger, 'info', '系统', '预约处理', "没有正在预约中的记录")
        return

    log_with_user(logger, 'info', '系统', '预约处理', f"开始处理预约列表，共 {len(active_list)} 条")
    for item in active_list:
        log_with_user(logger, 'info', item['pid'], '预约处理', f"预约学号: {item['pid']}, 优先级: {item['priority']}")
        reservation(item)
    log_with_user(logger, 'info', '系统', '预约处理', "预约处理结束")

def late_protect_action(user: Dict[str, Any], dev_name: str, seat_dict: Dict[str, Any]) -> None:
    """
    执行迟到保护动作

    迟到保护流程：
    1. 取消原预约
    2. 计算新的预约时间（延后1小时）
    3. 重新预约座位（最多重试3次）
    """
    pid = user["pid"]
    try:
        # 取消原预约
        try:
            log_with_user(logger, 'info', pid, '迟到保护', f"开始处理座位 {dev_name} 的迟到保护")
            library = LibrarySystem(
                username=user["pid"],
                password=user["lib_password"],
                vpn_password=user["vpn_password"]
            )

            # 删除原预约
            success, message = library.delete_seat(seat_dict["uuid"])
            if not success:
                log_with_user(logger, 'error', pid, '迟到保护', f"取消原预约失败: {message}")
                return
            log_with_user(logger, 'info', pid, '迟到保护', f"成功取消原预约: {seat_dict['uuid']}")

        except Exception as e:
            log_with_user(logger, 'error', pid, '迟到保护', f"取消原预约异常: {str(e)}")
            return

        # 计算新的预约时间
        target_time = seat_dict['target_time']
        date_str, time_range = target_time.split(' ')
        begin_time_str, end_time_str = time_range.split('-')

        begin_time = datetime.strptime(f"{date_str} {begin_time_str}", "%Y-%m-%d %H:%M:%S")
        end_time = datetime.strptime(f"{date_str} {end_time_str}", "%Y-%m-%d %H:%M:%S")

        # 调整时间
        new_begin = begin_time + timedelta(hours=1)
        duration = (end_time - new_begin).total_seconds() / 3600
        new_end = end_time + timedelta(hours=1) if duration < 2 else end_time

        new_begin_str = f"{date_str} {new_begin.strftime('%H:%M:%S')}"
        new_end_str = f"{date_str} {new_end.strftime('%H:%M:%S')}"

        log_with_user(logger, 'info', pid, '迟到保护',
                     f"调整后的预约时间: {new_begin_str} - {new_end_str}")

        # 获取座位ID
        device = db.devices.find_one({"devName": dev_name}, {"_id": 0, "devId": 1})
        if not device:
            log_with_user(logger, 'error', pid, '迟到保护', f"未找到座位ID: {dev_name}")
            return

        seat_id = device["devId"]
        log_with_user(logger, 'info', pid, '迟到保护', f"座位 {dev_name} 的ID为: {seat_id}")

        # 重新预约（带重试机制）
        max_retries = 3
        retry_count = 0
        last_error = None

        while retry_count < max_retries:
            try:
                res_msg, _ = library.reserve_seat(
                    seat_list=[seat_id],
                    resv_begin_time=new_begin_str,
                    resv_end_time=new_end_str
                )

                # 检查预约结果是否成功
                if "成功" in res_msg or "预约成功" in res_msg:
                    log_with_user(logger, 'info', pid, '迟到保护',
                                f"重新预约成功 (第{retry_count + 1}次尝试): {res_msg}")
                    # 新增：同步 owned_seat，保证多次保护
                    try:
                        library.get_reservation_info()
                        log_with_user(logger, 'info', pid, '迟到保护', "已同步最新预约信息到数据库")
                    except Exception as e:
                        log_with_user(logger, 'warning', pid, '迟到保护', f"同步预约信息失败: {str(e)}")
                    return
                else:
                    last_error = res_msg
                    log_with_user(logger, 'warning', pid, '迟到保护',
                                f"预约返回非成功状态 (第{retry_count + 1}次尝试): {res_msg}")

            except Exception as e:
                last_error = str(e)
                log_with_user(logger, 'error', pid, '迟到保护',
                            f"重新预约异常 (第{retry_count + 1}次尝试): {str(e)}")

            retry_count += 1
            if retry_count < max_retries:
                log_with_user(logger, 'info', pid, '迟到保护',
                            f"等待5秒后进行第{retry_count + 1}次重试...")
                time.sleep(5)

        # 所有重试都失败
        log_with_user(logger, 'error', pid, '迟到保护',
                     f"重新预约失败，已重试{max_retries}次，最后一次错误: {last_error}")
            
    except Exception as e:
        log_with_user(logger, 'error', pid, '迟到保护', f"执行失败: {str(e)}")

def schedule_late_protection_jobs() -> None:
    """
    注册并执行迟到保护任务
    
    工作流程：
    1. 获取所有开启迟到保护的用户
    2. 扫描每个用户的预约记录
    3. 为每个需要保护的座位注册保护任务
    4. 启动调度器并等待执行
    
    保护任务在预约时间前7分钟触发。
    调度器会一直运行到晚上22点。
    每30分钟重新扫描一次预约记录，确保新预约也能得到保护。
    """
    scheduler = BackgroundScheduler()
    
    def register_protection_jobs():
        """注册所有需要保护的预约任务"""
        now = datetime.now()
        today_str = now.strftime("%Y-%m-%d")
        
        try:
            # 获取需要保护的用户
            users = list(user_config_info.find({"late_protection": "True"}))
            log_with_user(logger, 'info', '系统', '迟到保护', f"找到 {len(users)} 个开启迟到保护的用户")
            
            # 注册保护任务
            for user in users:
                pid = user.get("pid")
                owned_seat = user.get("owned_seat", {})
                
                for dev_name, seat_list in owned_seat.items():
                    for seat_dict in seat_list:
                        if seat_dict['target_time'][:10] != today_str:
                            continue
                            
                        begin_str = seat_dict['target_time'][:19]
                        begin_time = datetime.strptime(begin_str, "%Y-%m-%d %H:%M:%S")
                        exec_time = begin_time - timedelta(minutes=7)
                        
                        # 只注册未来的任务
                        if exec_time > now:
                            job_id = f"{pid}_{dev_name}_{seat_dict['uuid']}"
                            scheduler.add_job(
                                late_protect_action,
                                'date',
                                run_date=exec_time,
                                args=[user, dev_name, seat_dict],
                                id=job_id,
                                replace_existing=True
                            )
                            log_with_user(logger, 'info', pid, '迟到保护', 
                                         f"注册任务 用户:{pid} 座位:{dev_name} 执行时间:{exec_time.strftime('%H:%M:%S')}")
                        else:
                            log_with_user(logger, 'info', pid, '迟到保护', 
                                          f"跳过过期任务 用户:{pid} 座位:{dev_name} 原执行时间:{exec_time.strftime('%H:%M:%S')}")
        except Exception as e:
            log_with_user(logger, 'error', '系统', '迟到保护', f"注册保护任务时发生异常: {str(e)}")

    try:
        # 启动调度器
        scheduler.start()
        log_with_user(logger, 'info', '系统', '迟到保护', "迟到保护 >> 调度器已启动")
        
        # 立即执行一次任务注册
        register_protection_jobs()
        
        # 添加定期重新注册任务
        scheduler.add_job(
            register_protection_jobs,
            'interval',
            minutes=30,
            id='refresh_protection_jobs',
            replace_existing=True
        )
        
        # 主循环
        while True:
            now = datetime.now()
            if now.hour >= 22:
                log_with_user(logger, 'info', '系统', '迟到保护', "到达22:00，准备退出...")
                scheduler.shutdown()
                break
            time.sleep(30)
            
    except Exception as e:
        log_with_user(logger, 'error', '系统', '迟到保护', f"注册任务时发生异常: {str(e)}")
        if scheduler.running:
            scheduler.shutdown()
    finally:
        log_with_user(logger, 'info', '系统', '迟到保护', "迟到保护服务已停止")

if __name__ == "__main__":
    """
    主程序入口
    
    执行流程：
    1. 处理所有预约请求
    2. 启动迟到保护服务
    
    异常处理：
    - 捕获所有异常并记录日志
    - 确保程序正常退出
    """
    try:
        process_reservations()
        schedule_late_protection_jobs()
    except KeyboardInterrupt:
        log_with_user(logger, 'info', '系统', '程序中断', "程序被用户中断")
    except Exception as e:
        log_with_user(logger, 'error', '系统', '程序异常', f"程序运行出错: {str(e)}")
    finally:
        log_with_user(logger, 'info', '系统', '程序退出', "程序已退出")
