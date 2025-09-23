# config.py
from dotenv import load_dotenv
import os
from pathlib import Path

# 获取项目根目录
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = Path(__file__).parent.parent

# 加载 .env 文件
load_dotenv(dotenv_path=os.path.join(PROJECT_ROOT, '.env'))

# 定义全局变量
VPN_PASSWORD = os.getenv("VPN_PASSWORD")
VPN_USERNAME = os.getenv("VPN_USERNAME")

# 服务器配置
SERVER_IP = os.getenv("SERVER_IP", "127.0.0.1:5001")
DB_IP = os.getenv("DB_IP", "103.38.80.17:27018")

# 日志配置
LOG_FILE = os.getenv("LOG_FILE", "logs/auto_lib.log")  # 添加默认值
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")  # 添加默认日志级别

# 确保日志目录存在
LOG_DIR = os.path.join(PROJECT_ROOT, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

# 完整的日志文件路径
LOG_FILE = os.path.join(PROJECT_ROOT, LOG_FILE)

# 其他配置
MAX_RETRY = int(os.getenv("MAX_RETRY", "3"))  # 最大重试次数
RETRY_INTERVAL = int(os.getenv("RETRY_INTERVAL", "5"))  # 重试间隔（秒）

# 数据库配置
DB_NAME = "AutoLib"  # 固定使用 AutoLib 作为数据库名
FOLDER_PATH = os.path.join(PROJECT_ROOT, os.getenv("FOLDER_PATH"))

if __name__ == "__main__":
    print(f"Log File Path: {LOG_FILE}")
    print(f"Database Path: {DB_NAME}")
    print(f"Folder Path: {FOLDER_PATH}")