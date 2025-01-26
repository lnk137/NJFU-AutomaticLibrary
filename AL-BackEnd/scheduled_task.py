#linux:  #!/www/wwwroot/RemoteLibrary/.venv/bin/python
from utils.library_system import LibrarySystem
from utils.library_database import LibraryDatabase
from utils.vpn_system import VPNSystem
from utils import config
import requests
import os
import json
import logging

log_path = os.path.dirname(config.LOG_FILE)
if not os.path.exists(log_path):
    os.makedirs(log_path)
# 配置日志
logging.basicConfig(
    level=logging.INFO,  # 设置日志级别
    format="[%(asctime)s] [%(levelname)s] [%(name)s] - %(message)s",
    handlers=[
        logging.FileHandler(config.LOG_FILE, encoding="utf-8"),  # 将日志输出到文件，设置编码为 utf-8
        logging.StreamHandler()  # 同时输出到控制台
    ]
)


# 调用日志配置函数
logger = logging.getLogger(__name__)
# 创建一个快捷方式用于记录日志
log = logger.info

def get_seat_ids(db, seat_list):
    """
    根据设备名称列表从数据库中获取座位的设备 ID。

    :param db: 数据库对象
    :param seat_list: 设备名称列表
    :return: 座位设备 ID 列表
    """
    seat_id_list = []
    for device_name in seat_list:
        device_id = db.get_device_id_by_name(device_name)
        if device_id:
            seat_id_list.append(device_id)
        else:
            log(f"设备号 {device_name} 不存在")
    return seat_id_list


def handle_reservation_error(e, reservation_item):
    """
    处理预约过程中出现的异常。

    :param e: 异常对象
    :param reservation_item: 当前预约项
    """
    log(f"处理预约 {reservation_item['pid']} 时出现异常: {str(e)}")


def insert_user_info(db, user_info):
    """
    将用户信息插入数据库。

    :param db: 数据库对象
    :param user_info: 用户信息字典
    """
    # 检查字段数量是否符合预期
    db.insert_user(user_info)



def insert_reservation_result(db, user_pid, success_message, fail_message):
    """
    插入或更新预约结果到数据库。

    :param db: 数据库对象
    :param user_pid: 用户 PID
    :param success_message: 成功预约的消息
    :param fail_message: 失败预约的消息
    """
    res_message = f"{success_message},{fail_message}"
    log(f"预约结果: {res_message}")
    db.insert_or_update_reservation_result(user_pid, res_message)


def reservation(db, reservation_item):
    """
    处理单个座位预约的逻辑，包括：
    1. 从数据库获取设备 ID。
    2. 使用图书馆系统进行预约。
    3. 保存预约结果到数据库。

    :param db: 数据库对象
    :param reservation_item: 包含预约信息的字典
    """
    logonName = reservation_item["logonName"]
    password = reservation_item["password"]
    seat_list = json.loads(reservation_item["seat_list"])
    begin_time = reservation_item["begin_time"]
    end_time = reservation_item["end_time"]

    # 获取座位设备 ID
    seat_id_list = get_seat_ids(db, seat_list)
    shared_session = requests.Session()
    vpn=VPNSystem(config.VPN_USERNAME, config.VPN_PASSWORD)
    vpn.session = shared_session

    # 创建 LibrarySystem 实例并进行 VPN 登录
    library = LibrarySystem(logonName, password)
    library.session = shared_session

    if not vpn.vpn_login():
        log(f"VPN 登录失败，无法继续预约")
        return

    try:
        # 进行座位预约
        success_message, user_info, fail_message = library.reserve_seat(seat_list=seat_id_list, begin_time=begin_time,
                                                                        end_time=end_time)

    except Exception as e:
        handle_reservation_error(e, reservation_item)
        return

    if len(user_info) != 9:
        log(f"登录失败: {user_info}")
        return
    # 插入用户信息和预约结果到数据库
    insert_user_info(db, user_info)
    insert_reservation_result(db, user_info["pid"], success_message, fail_message)


def process_reservations(db):
    """
    处理所有预约记录的执行流程：
    1. 查询数据库中所有正在预约的记录。
    2. 按优先级处理预约。
    3. 为每个记录调用 reservation 函数进行处理。

    :param db: 数据库对象
    :return: None
    """
    # 查询所有正在预约中的记录
    active_reservations = db.get_all_active_reservations()

    if not active_reservations:
        log("没有正在预约中的记录")
        return

    # 按优先级从大到小排序预约记录
    active_reservations = sorted(active_reservations, key=lambda x: x["priority"], reverse=True)

    log("-------"*10)
    for reservation_item in active_reservations:
        try:
            log("\n\n")
            log(f"预约学号: {reservation_item['pid']},优先级: {reservation_item['priority']}")
            reservation(db, reservation_item)
        except Exception as e:
            handle_reservation_error(e, reservation_item)
    log("-------"*10)

def main():
    """
    主函数：执行预约处理。
    """
    db = LibraryDatabase()

    try:
        process_reservations(db)
    finally:
        # 确保数据库连接被关闭
        db.close()


if __name__ == "__main__":
    main()
