from flask import Blueprint
from utils.library_system import *
reserve_bp = Blueprint("reserve_bp", __name__)


@reserve_bp.route("/do", methods=["GET"])
def do():
    pass
    # # 根据学号填充信息
    # library = LibrarySystem("xx", "xx!")
    # library.vpn_login()
    # _,user_info=library.reserve_seat(seat_list=["100455862"],begin_time="10:30:00", end_time="22:00:00")
    #
    # # 调用 insert_user 插入数据
    # insert_user(user_info)


if __name__ == "__main__":
    do()