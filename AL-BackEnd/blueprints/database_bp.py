from flask import Blueprint, jsonify, request
from pymongo import MongoClient, DESCENDING
from datetime import datetime
from utils import config

# Blueprint and database setup
database_bp = Blueprint("database_bp", __name__)
mongo_client = MongoClient(f"mongodb://{config.DB_IP}/")
db = mongo_client.AutoLib
user_cfg = db.user_config_info
ann = db.announcements


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
    """
    data, err = get_json_or_400()
    if err:
        return jsonify(*err)
    if 'pid' not in data:
        return jsonify({"error": "缺少 pid"}), 400

    # 直接使用整个 data 作为文档内容，并追加更新时间
    rec = data.copy()
    rec['updated_at'] = datetime.utcnow()

    result, code = upsert_collection(user_cfg, {"pid": rec['pid']}, rec)
    return jsonify(result), code

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
