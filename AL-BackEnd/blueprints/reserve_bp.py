from flask import Blueprint
from utils.library_system import *
from utils.library_database import *
reserve_bp = Blueprint("reserve_bp", __name__)


@reserve_bp.route("/do", methods=["GET"])
def do():

    # 根据学号填充信息
    library = LibrarySystem("2210104201", "njfu241016!")
    library.vpn_login()
    _,user_info=library.reserve_seat(seat_list=["100455862"],begin_time="10:30:00", end_time="22:00:00")

    # 初始化数据库连接
    db = LibraryDatabase()

    # 调用 insert_user 插入数据
    db.insert_user(user_info)

    # 关闭数据库连接
    db.close()
if __name__ == "__main__":
    do()