from flask import Blueprint, jsonify, request
from utils import config

app_bp = Blueprint("app_bp", __name__)

# 最新版本信息
LATEST_VERSION = {
    "version": "3.1.5",  # 版本号
    "versionCode": 315,  # 版本码
    "downloadUrl": "https://103.38.80.17:15079/down/4weDx7DlR52G.apk",  # 下载地址
    "updateLog": """
修复优先级错误
    """.strip()
}


@app_bp.route("/check_update", methods=["GET"])
def check_update():
    """
    检查应用更新

    请求参数:
    - currentVersion: 当前版本号
    - currentVersionCode: 当前版本码

    返回:
    {
        "hasUpdate": true/false,
        "latestVersion": "最新版本号",
        "latestVersionCode": 最新版本码,
        "downloadUrl": "下载地址",
        "updateLog": "更新日志"
    }
    """
    try:
        current_version = request.args.get("currentVersion")
        current_version_code = request.args.get("currentVersionCode")

        if not current_version or not current_version_code:
            return jsonify({"error": "缺少版本信息"}), 400

        # 简单比较版本号是否不同
        has_update = current_version != LATEST_VERSION["version"]

        print(f"版本检查结果：")
        print(f"当前版本：{current_version} (code: {current_version_code})")
        print(f"最新版本：{LATEST_VERSION['version']} (code: {LATEST_VERSION['versionCode']})")
        print(f"是否需要更新：{'是' if has_update else '否'}")

        return jsonify({
            "hasUpdate": has_update,
            "latestVersion": LATEST_VERSION["version"],
            "latestVersionCode": LATEST_VERSION["versionCode"],
            "downloadUrl": LATEST_VERSION["downloadUrl"],
            "updateLog": LATEST_VERSION["updateLog"]
        }), 200

    except Exception as e:
        print(f"检查更新失败: {str(e)}")
        return jsonify({"error": f"检查更新失败: {str(e)}"}), 500