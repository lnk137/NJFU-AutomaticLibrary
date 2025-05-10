
from pymongo import MongoClient

from utils import config
client = MongoClient(f"mongodb://{config.DB_IP}/")

# 选择数据库（test）和集合（students）
db = client["AutoLib"]
collection = db["students"]

# 插入一条测试文档
student_data = {
    "name": "张三",
    "age": 20,
    "major": "计算机科学",
    "score": 92.5
}

result = collection.insert_one(student_data)

print(f"插入成功，文档ID：{result.inserted_id}")
