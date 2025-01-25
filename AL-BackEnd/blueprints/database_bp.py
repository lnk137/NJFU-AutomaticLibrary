from flask import Blueprint, jsonify, request
from utils.library_database import *
from datetime import datetime
database_bp = Blueprint("database_bp", __name__)

# 插入预约信息
@database_bp.route("/insert_reservation", methods=["POST"])
def insert_reservation():
    # 初始化数据库连接
    db = LibraryDatabase()

    try:
        # 获取前端数据
        user_info = request.get_json()
        if not user_info:
            return jsonify({"error": "无效的请求数据"}), 400

        # 检查必需字段
        required_fields = ["pid", "logonName", "password", "timeSlot", "seat_list"]
        for field in required_fields:
            if field not in user_info:
                return jsonify({"error": f"缺少必需字段: {field}"}), 400

        # 处理时间段与 seat_list
        user_info.update({
            "begin_time": user_info["timeSlot"].split("-")[0],
            "end_time": user_info["timeSlot"].split("-")[1],
            "seat_list": json.dumps(user_info["seat_list"]),  # 转换 seat_list 为 JSON 字符串
            "is_reserved": int(user_info.get("is_reserved", True))  # 默认值为 True
        })

        # 删除不需要的字段
        user_info.pop("timeSlot", None)

        # 调试日志
        print("接收到的预约数据:", user_info)

        # 插入数据
        db.insert_or_update_reservation(user_info)

        return jsonify({"message": "提交成功！"})

    except Exception as e:
        print(f"插入预约信息失败: {e}")
        return jsonify({"message": f"服务器错误"}), 500

    finally:
        # 确保数据库连接关闭
        db.close()

# 更新预约状态
@database_bp.route("/update_reservation_status", methods=["POST"])
def update_reservation_status():
    try:
        # 初始化数据库连接
        db = LibraryDatabase()

        # 获取来自前端的数据（pid, is_reserved）
        res = request.get_json()
        pid = res['pid']
        is_reserved = res['is_reserved']
        # 更新用户预约信息
        db.update_reservation_status(pid, is_reserved)

        # 关闭数据库连接
        db.close()
        return jsonify({
            "message": "已更新",
        })
    except Exception as e:
        print(f"更新预约状态失败: {e}")
        return jsonify({"message": f"服务器错误"}), 500

    finally:
        # 确保数据库连接关闭
        db.close()


# 更新优先级
@database_bp.route("/update_priority", methods=["POST"])
def update_priority():
    try:
        # 初始化数据库连接
        db = LibraryDatabase()

        # 获取来自前端的数据（pid, is_reserved）
        res = request.get_json()
        pid = res['pid']
        priority = res['priority']
        # 更新用户预约信息
        db.update_priority_by_pid(pid, priority)

        # 关闭数据库连接
        db.close()
        return jsonify({
            "message": "已更新",
        })
    except Exception as e:
        print(f"更新预约状态失败: {e}")
        return jsonify({"message": f"服务器错误"}), 500

    finally:
        # 确保数据库连接关闭
        db.close()

# 查询预约结果
@database_bp.route("/get_reservations_by_pid", methods=["POST"])
def get_reservations_by_pid():
    # 初始化数据库连接
    db = LibraryDatabase()

    # 获取来自前端的数据（pid）
    pid = request.get_json().get("pid")

    # 查询预约信息
    reservation_result = db.get_reservation_result_by_pid(pid)

    # 关闭数据库连接
    db.close()
    return jsonify({
        "message": reservation_result,
    })

# 执行自定义 SQL 查询
@database_bp.route("/execute_sql", methods=["POST"])
def execute_sql():
    try:
        # 初始化数据库连接
        db = LibraryDatabase()

        # 获取前端数据
        data = request.get_json()
        sql = data.get("sql")  # 获取 SQL 语句
        params = data.get("params", [])  # 获取查询参数，默认为空列表

        if not sql:
            return jsonify({"error": "缺少 SQL 语句"}), 400

        # 调试日志
        print("执行的 SQL:", sql)
        print("参数:", params)

        # 执行查询
        db.cursor.execute(sql, params)
        rows = db.cursor.fetchall()
        columns = [desc[0] for desc in db.cursor.description]  # 获取列名

        # 格式化查询结果
        results = [dict(zip(columns, row)) for row in rows]

        return jsonify({
            "message": "查询成功",
            "results": results
        })
    except Exception as e:
        print(f"执行自定义 SQL 失败: {e}")
        return jsonify({"error": f"服务器错误: {str(e)}"}), 500
    finally:
        # 确保数据库连接关闭
        db.close()

@database_bp.route("/insert_announcement", methods=["POST"])
def insert_or_update_announcement():
    db = LibraryDatabase()

    try:
        # 获取前端数据
        data = request.get_json()
        if not data:
            return jsonify({"error": "无效的请求数据，未提供 JSON 数据"}), 400

        # 检查必需字段
        required_fields = ["title", "content", "importance"]
        missing_fields = [field for field in required_fields if field not in data]
        if missing_fields:
            return jsonify({"error": f"缺少必需字段: {', '.join(missing_fields)}"}), 400

        # 自动生成时间戳
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        data["publish_time"] = data.get("publish_time", current_time)  # 如果没有传递 publish_time，则设置为当前时间
        data["update_time"] = current_time  # 每次插入或更新都重新设置 update_time

        # 插入或更新公告
        db.insert_announcement(data)

        return jsonify({"message": "公告插入或更新成功！"}), 200
    except KeyError as e:
        # 捕获 KeyError（如果 data 中缺少键）
        print(f"插入或更新公告失败，缺少字段: {e}")
        return jsonify({"error": f"服务器错误，缺少字段: {e}"}), 500
    except Exception as e:
        # 捕获其他异常
        print(f"插入或更新公告失败: {e}")
        return jsonify({"error": f"服务器错误: {str(e)}"}), 500
    finally:
        # 确保数据库连接关闭
        db.close()


# 获取公告
@database_bp.route("/get_announcements", methods=["GET"])
def get_announcements():
    db = LibraryDatabase()

    try:
        # 获取查询参数
        importance = request.args.get("importance")  # 可选参数（高, 中, 低）

        # 查询公告
        announcements = db.get_announcements(importance)

        return jsonify({
            "message": "查询成功",
            "announcements": announcements
        })
    except Exception as e:
        print(f"查询公告失败: {e}")
        return jsonify({"error": "服务器错误"}), 500
    finally:
        db.close()



