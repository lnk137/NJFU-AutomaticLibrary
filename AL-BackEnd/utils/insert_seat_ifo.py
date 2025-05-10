# -*- coding: utf-8 -*-
import os
from pymongo import MongoClient
from utils import config
def insert_devices_from_folder_to_mongo(folder_path, mongo_uri, db_name):
    """
    遍历文件夹中的所有 .txt 文件，将设备信息插入 MongoDB。
    :param folder_path: 包含设备信息的 .txt 文件的文件夹路径
    :param mongo_uri: MongoDB 连接 URI
    :param db_name: 要使用的数据库名称
    """
    def insert_devices_from_txt(txt_file_path, collection):
        """
        从 txt 文件读取设备信息并插入 MongoDB collection。
        :param txt_file_path: txt 文件路径
        :param collection: PyMongo Collection 对象
        """
        # 从文件名提取 location，例如 “room01.txt” -> “room”
        filename = os.path.basename(txt_file_path)
        location = filename[:-4]           # 去掉 ".txt"
        location = location[:-2] if len(location) > 2 else location

        with open(txt_file_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue

                # 假设格式 "devId: 0001, devName: ScannerA"
                parts = [p.strip() for p in line.split(",")]
                devId = parts[0].split(":")[1].strip()
                devName = parts[1].split(":")[1].strip()

                device_doc = {
                    "devId": devId,
                    "devName": devName,
                    "location": location
                }
                # 插入或更新：如果同一个 devId 已存在就更新
                collection.update_one(
                    {"devId": devId},
                    {"$set": device_doc},
                    upsert=True
                )

        print(f"[MongoDB] 已处理文件 {filename}，location={location}")

    # 连接 MongoDB
    client = MongoClient(mongo_uri)
    db = client[db_name]
    devices_col = db.devices

    # 遍历并插入
    try:
        for fn in os.listdir(folder_path):
            if fn.lower().endswith(".txt"):
                insert_devices_from_txt(
                    os.path.join(folder_path, fn),
                    devices_col
                )
        print("[MongoDB] 所有设备信息已成功插入或更新。")
    except Exception as e:
        print(f"[MongoDB] 处理文件夹时出错: {e}")
    finally:
        # 打印一下目前集合中的所有文档数
        count = devices_col.count_documents({})
        print(f"[MongoDB] 当前 devices 集合文档数量: {count}")
        client.close()


if __name__ == "__main__":
    # 本地或远程 MongoDB URI

    mongo_uri = f"mongodb://{config.DB_IP}/"
    # 使用的数据库名
    db_name = "AutoLib"
    # 包含 .txt 的文件夹
    folder_path = r"E:\AAAAAAAA\FrontBackEndProjects\NJFU-AutomaticLibrary\AL-BackEnd\座位信息"

    insert_devices_from_folder_to_mongo(folder_path, mongo_uri, db_name)
