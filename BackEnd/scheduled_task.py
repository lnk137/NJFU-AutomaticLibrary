#linux:  #!/www/wwwroot/RemoteLibrary/.venv/bin/python
from utils.LibrarySystem import *
from utils.LibraryDatabase import *
import json
import logging
from config.config import *
def setup_logging(log_file=log_file, log_level=logging.INFO):
    """
    设置日志配置，输出到文件和控制台，支持 UTF-8 编码。

    :param log_file: 日志文件的路径，默认 'app.log'
    :param log_level: 日志级别，默认 INFO
    """
    # 设置日志格式
    log_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

    # 创建一个日志记录器
    logger = logging.getLogger()

    # 设置日志级别
    logger.setLevel(log_level)

    # 创建一个文件处理器，将日志保存到 app.log 文件，并设置编码格式为 utf-8
    file_handler = logging.FileHandler(log_file, mode='a', encoding='utf-8')
    file_handler.setFormatter(log_formatter)

    # 创建一个控制台处理器，将日志输出到控制台
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(log_formatter)

    # 将文件处理器和控制台处理器添加到记录器
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    # 返回 logger，方便之后使用
    return logger

# 调用日志配置函数
logger = setup_logging()
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

    # 创建 LibrarySystem 实例并进行 VPN 登录
    library = LibrarySystem(logonName, password)
    if not library.vpn_login():
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
