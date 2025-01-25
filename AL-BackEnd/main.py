from flask import Flask
from flask_cors import CORS
from utils import config
from blueprints.reserve_bp import reserve_bp
from blueprints.database_bp import database_bp
from utils.insert_seat_ifo import *
app = Flask(__name__)

# 配置全局跨域
CORS(app)

# 注册蓝图
app.register_blueprint(reserve_bp, url_prefix="/reserve")
app.register_blueprint(database_bp, url_prefix="/db")

@app.route("/")
def home():
    return "Welcome to the Flask App with CORS!"

if __name__ == "__main__":
    insert_devices_from_folder_to_db(folder_path=config.FOLDER_PATH, db_path=config.DB_NAME)
    # 指定端口为 5001
    app.run(debug=True, host="0.0.0.0", port=5001)
