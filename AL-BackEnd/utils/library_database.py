from pymongo import MongoClient

from utils import config

class MongoDBHandler:
    def __init__(self, uri, database_name):
        """
        初始化 MongoDB 连接。

        :param uri: MongoDB 连接字符串
        :param database_name: 数据库名称
        """
        self.client = MongoClient(uri)
        self.db = self.client[database_name]

    def initialize_collections(self):
        """
        初始化集合结构。
        模拟表格：
        - user_info: 存储用户信息
        - reservation_info: 存储预约信息
        - devices: 存储设备信息
        - reservation_result: 存储预约结果
        - announcement_info: 存储公告信息
        """
        # 初始化集合并设置索引（MongoDB 中索引类似于关系型数据库的主键）

        # user_info 集合
        user_info_collection = self.db["user_info"]
        user_info_collection.create_index("pid", unique=True)  # 创建唯一索引，相当于主键

        # reservation_info 集合
        reservation_info_collection = self.db["reservation_info"]
        reservation_info_collection.create_index("reservation_id", unique=True)

        # devices 集合
        devices_collection = self.db["devices"]
        devices_collection.create_index("device_id", unique=True)

        # reservation_result 集合
        reservation_result_collection = self.db["reservation_result"]
        reservation_result_collection.create_index("result_id", unique=True)

        # announcement_info 集合
        announcement_info_collection = self.db["announcement_info"]
        announcement_info_collection.create_index("announcement_id", unique=True)

        print("所有集合初始化完成")

if __name__ == "__main__":
    # 示例使用
    mongo_handler = MongoDBHandler(uri=config.DB_PATH, database_name="library_system")
    mongo_handler.initialize_collections()

