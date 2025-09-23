from flask import Blueprint, jsonify, request
from pymongo import MongoClient, DESCENDING
from datetime import datetime
from utils import config
from utils.library_system import LibrarySystem

# Blueprint and database setup
database_bp = Blueprint("database_bp", __name__)

# 构建 MongoDB 连接字符串，暂时去掉密码验证
# WARNING: 这会禁用应用层面的数据库认证，带来安全风险，仅用于调试！
# 在生产环境中应启用并正确配置数据库认证
mongo_uri = f"mongodb://{config.DB_IP}/"

try:
    print(f"正在连接 MongoDB: {mongo_uri}")
    mongo_client = MongoClient(mongo_uri)
    # 测试连接，如果认证在服务器端开启，此处仍可能失败
    print("正在测试连接...")
    mongo_client.admin.command('ping')
    print(f"正在选择数据库: {config.DB_NAME}")
    db = mongo_client[config.DB_NAME]  # 使用配置的数据库名
    user_cfg = db.user_config_info
    ann = db.announcements
    print("✅ MongoDB 连接成功！(应用未启用密码验证)")
except Exception as e:
    import traceback

    print("=" * 50)
    print("❌ MongoDB 连接失败！")
    print(f"错误类型: {type(e).__name__}")
    print(f"错误信息: {str(e)}")
    print("详细堆栈:")
    print(traceback.format_exc())
    print("=" * 50)
    print("提示：MongoDB 服务器可能仍然需要认证，或者连接地址/端口有误。")


def get_json_or_400():
    """统一的 JSON 获取和校验"""
    data = request.get_json(silent=True)
    if not data:
        return None, ({"error": "无效的请求数据"}, 400)
    return data, None


def upsert_collection(collection, key_filter: dict, new_values: dict):
    """统一的 upsert 操作，捕获异常并返回状态"""
    try:
        collection.update_one(key_filter, {"$set": new_values}, upsert=True)
        return {"message": "操作成功！"}, 200
    except Exception as e:
        return {"error": str(e)}, 500


def update_field(collection, pid, field_name, field_value):
    """更新单个字段并记录更新时间"""
    record = {field_name: field_value, "updated_at": datetime.utcnow()}
    return upsert_collection(collection, {"pid": pid}, record)


@database_bp.route("/reservation/all", methods=["POST"])
def insert_full_reservation():
    """
    测试接口：插入或更新完整的预约配置，包括所有字段
    接收 JSON:
    {
      "pid": "...",
      "vpn_password": "...",
      "lib_password": "...",
      "seat_list": [...],
      "mode": "...",
      "time": {...},
      "priority": ...,
      "is_reserved": ...
    }
    返回:
    {
      "message": "预约配置已更新",
      "data": {
        // 更新后的完整数据
      }
    }
    """
    data, err = get_json_or_400()
    if err:
        return jsonify(*err)
    if 'pid' not in data:
        return jsonify({"error": "缺少 pid"}), 400

    # 获取原有数据
    existing_data = user_cfg.find_one({"pid": data["pid"]})

    # 如果存在原有数据，则合并新旧数据
    if existing_data:
        # 删除MongoDB的_id字段
        if "_id" in existing_data:
            del existing_data["_id"]

        # 特殊处理priority字段
        if "priority" not in existing_data:
            existing_data["priority"] = 0

        # 更新原有数据中的字段
        for key, value in data.items():
            if value is not None:  # 只更新非None的字段
                existing_data[key] = value
        # 更新更新时间
        existing_data['updated_at'] = datetime.utcnow()
        # 使用更新后的数据
        rec = existing_data
    else:
        # 如果是新数据，直接使用
        rec = data.copy()
        # 如果是新用户且没有priority字段，设置为0
        if "priority" not in rec:
            rec["priority"] = 0
        rec['updated_at'] = datetime.utcnow()

    # 更新数据库
    user_cfg.update_one(
        {"pid": data["pid"]},
        {"$set": rec},
        upsert=True
    )

    # 返回更新后的完整数据
    return jsonify({
        "message": "预约配置已更新",
        "data": rec
    })


@database_bp.route("/reservation", methods=["POST"])
def insert_or_update_reservation():
    """
    插入或更新用户配置（预约信息）
    包括：vpn_password、lib_password、seat_list
    """
    data, err = get_json_or_400()
    if err:
        return jsonify(*err)

    required = ["pid", "vpn_password", "lib_password", "seat_list"]
    missing = [f for f in required if f not in data]
    if missing:
        return jsonify({"error": f"缺少字段: {', '.join(missing)}"}), 400

    rec = {
        "pid": data["pid"],
        "vpn_password": data["vpn_password"],
        "lib_password": data["lib_password"],
        "seat_list": data["seat_list"],
    }
    result, code = upsert_collection(user_cfg, {"pid": rec["pid"]}, rec)
    return jsonify(result), code


@database_bp.route("/reservation/time", methods=["POST"])
def set_time_reservation():
    """
    设置预约时间段和模式
    """
    data, err = get_json_or_400()
    if err:
        return jsonify(*err)
    if "pid" not in data or "mode" not in data or "timeSlot" not in data:
        return jsonify({"error": "缺少 pid、mode 或 timeSlot"}), 400

    try:
        begin, end = data["timeSlot"].split("-")
    except ValueError:
        return jsonify({"error": "timeSlot 格式应为 '开始-结束'"}), 400

    rec = {
        "mode": data["mode"],
        "time": {"begin": begin, "end": end},
    }
    result, code = upsert_collection(user_cfg, {"pid": data["pid"]}, rec)
    return jsonify(result), code


@database_bp.route("/reservation/status", methods=["POST"])
def update_reservation_status():
    """
    更新预约状态
    """
    data, err = get_json_or_400()
    if err:
        return jsonify(*err)

    pid = data.get("pid")
    status = data.get("is_reserved")
    if pid is None or status is None:
        return jsonify({"error": "缺少 pid 或 is_reserved"}), 400

    result, code = update_field(user_cfg, pid, "is_reserved", status)
    return jsonify(result), code


@database_bp.route("/reservation/priority", methods=["POST"])
def update_priority():
    """
    更新优先级
    """
    data, err = get_json_or_400()
    if err:
        return jsonify(*err)

    pid = data.get("pid")
    prio = data.get("priority")
    if pid is None or prio is None:
        return jsonify({"error": "缺少 pid 或 priority"}), 400

    result, code = update_field(user_cfg, pid, "priority", prio)
    return jsonify(result), code


@database_bp.route("/reservation/query", methods=["POST"])
def get_reservation_by_pid():
    """
    根据 pid 查询预约记录
    """
    data, err = get_json_or_400()
    if err:
        return jsonify(*err)

    pid = data.get("pid")
    if not pid:
        return jsonify({"error": "缺少 pid"}), 400

    rec = user_cfg.find_one({"pid": pid}, {"_id": 0})
    return jsonify({"message": rec or {}}), 200


@database_bp.route("/query", methods=["POST"])
def execute_query():
    """
    任意集合查询
    POST body 包含：collection, filter, projection
    """
    data, err = get_json_or_400()
    if err:
        return jsonify(*err)

    coll_name = data.get("collection")
    if not coll_name:
        return jsonify({"error": "缺少 collection 字段"}), 400

    try:
        coll = db[coll_name]
        cursor = coll.find(data.get("filter", {}), data.get("projection"))
        results = [{k: v for k, v in doc.items() if k != "_id"} for doc in cursor]
        return jsonify({"message": "查询成功", "results": results}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@database_bp.route("/announcement", methods=["POST"])
def insert_or_update_announcement():
    """
    插入或更新公告
    """
    data, err = get_json_or_400()
    if err:
        return jsonify(*err)

    required = ["title", "content", "importance"]
    missing = [f for f in required if f not in data]
    if missing:
        return jsonify({"error": f"缺少字段: {', '.join(missing)}"}), 400

    now = datetime.utcnow()
    rec = {
        "title": data["title"],
        "content": data["content"],
        "importance": data["importance"],
        "publish_time": data.get("publish_time", now),
        "update_time": now
    }
    result, code = upsert_collection(ann, {"title": rec["title"]}, rec)
    return jsonify(result), code


@database_bp.route("/announcement", methods=["GET"])
def get_announcements():
    """
    根据 importance（可选）获取公告列表，按 publish_time 降序
    """
    importance = request.args.get("importance")
    filter_ = {"importance": importance} if importance else {}
    try:
        cursor = ann.find(filter_).sort("publish_time", DESCENDING)
        result = [{k: v for k, v in doc.items() if k != "_id"} for doc in cursor]
        return jsonify({"message": "查询成功", "announcements": result}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@database_bp.route("/reservation/query_info", methods=["POST"])
def query_reservation_info():
    """
    查询用户的预约信息

    请求体:
    {
        "pid": "学号",
        "vpn_password": "VPN密码",
        "lib_password": "图书馆密码",
        "begin_date": "开始日期(可选)",
        "end_date": "结束日期(可选)",
        "page": 页码(可选),
        "page_num": 每页记录数(可选)
    }

    返回:
    {
        "message": "查询结果消息",
        "reservations": [
            {
                "uuid": "预约ID",
                "resvBeginTime": "开始时间",
                "resvEndTime": "结束时间",
                "resvStatus": "预约状态",
                "resvName": "预约人姓名",
                "devInfo": {
                    "devName": "座位名称",
                    "roomName": "房间名称",
                    ...
                }
            },
            ...
        ]
    }
    """
    data, err = get_json_or_400()
    if err:
        return jsonify(*err)

    # 验证必要字段
    required = ["pid", "vpn_password", "lib_password"]
    missing = [f for f in required if f not in data]
    if missing:
        return jsonify({"error": f"缺少必要字段: {', '.join(missing)}"}), 400

    try:
        # 初始化图书馆系统
        library = LibrarySystem(
            username=data["pid"],
            password=data["lib_password"],
            vpn_password=data["vpn_password"]
        )

        # 获取查询参数
        begin_date = data.get("begin_date")
        end_date = data.get("end_date")
        page = int(data.get("page", 1))
        page_num = int(data.get("page_num", 10))

        # 查询预约信息
        reservations, message = library.get_reservation_info(
            begin_date=begin_date,
            end_date=end_date,
            page=page,
            page_num=page_num
        )

        if reservations is None:
            return jsonify({"error": message}), 500

        return jsonify({
            "message": message,
            "reservations": reservations
        }), 200

    except Exception as e:
        return jsonify({"error": f"查询预约信息失败: {str(e)}"}), 500


@database_bp.route("/reservation/delete", methods=["POST"])
def delete_reservation():
    """
    删除预约座位

    请求体:
    {
        "pid": "学号",
        "vpn_password": "VPN密码",
        "lib_password": "图书馆密码",
        "uuid": "预约记录的UUID"
    }

    返回:
    {
        "message": "操作结果消息",
        "success": true/false
    }
    """
    data, err = get_json_or_400()
    if err:
        return jsonify(*err)

    # 验证必要字段
    required = ["pid", "vpn_password", "lib_password", "uuid"]
    missing = [f for f in required if f not in data]
    if missing:
        return jsonify({"error": f"缺少必要字段: {', '.join(missing)}"}), 400

    try:
        # 初始化图书馆系统
        library = LibrarySystem(
            username=data["pid"],
            password=data["lib_password"],
            vpn_password=data["vpn_password"]
        )

        # 删除预约
        success, message = library.delete_seat(data["uuid"])

        return jsonify({
            "message": message,
            "success": success
        }), 200 if success else 500

    except Exception as e:
        return jsonify({
            "error": f"删除预约失败: {str(e)}",
            "success": False
        }), 500


@database_bp.route("/reservation/result", methods=["POST"])
def get_user_result():
    """
    查询用户的预约结果信息

    请求体:
    {
        "pid": "学号"
    }

    返回:
    {
        "message": "查询结果消息",
        "result": "预约结果信息"
    }
    """
    data, err = get_json_or_400()
    if err:
        return jsonify(*err)

    # 验证必要字段
    if "pid" not in data:
        return jsonify({"error": "缺少必要字段: pid"}), 400

    try:
        # 查询用户配置中的 result 字段
        user_record = user_cfg.find_one(
            {"pid": data["pid"]},
            {"result": 1, "_id": 0}
        )

        return jsonify({
            "message": "查询成功",
            "result": user_record.get("result", "") if user_record else ""
        }), 200

    except Exception as e:
        return jsonify({"error": f"查询预约结果失败: {str(e)}"}), 500
