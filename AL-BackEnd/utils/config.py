# config.py
from dotenv import load_dotenv
import os

# 获取项目根目录
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(BASE_DIR)

# 加载 .env 文件
load_dotenv(dotenv_path=os.path.join(PROJECT_ROOT, '.env'))

# 定义全局变量
VPN_PASSWORD = os.getenv("VPN_PASSWORD")
VPN_USERNAME = os.getenv("VPN_USERNAME")
LOG_FILE = os.path.join(PROJECT_ROOT, os.getenv("LOG_FILE"))
DB_NAME = os.path.join(PROJECT_ROOT, os.getenv("DB_NAME"))
FOLDER_PATH = os.path.join(PROJECT_ROOT, os.getenv("FOLDER_PATH"))
SERVER_IP = os.getenv("SERVER_IP")
DB_PATH=os.getenv("DB_PATH")
if __name__ == "__main__":
    print(f"Log File Path: {LOG_FILE}")
    print(f"Database Path: {DB_NAME}")
    print(f"Folder Path: {FOLDER_PATH}")
    print(f"DB_PATH: {DB_PATH}")