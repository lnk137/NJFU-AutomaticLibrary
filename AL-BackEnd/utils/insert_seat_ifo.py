# -*- coding: utf-8 -*-
import os
from utils.library_database import LibraryDatabase

def insert_devices_from_folder_to_db(folder_path, db_path):
    """
    遍历文件夹中的所有 .txt 文件，将设备信息插入数据库。
    :param folder_path: 包含设备信息的 .txt 文件的文件夹路径
    :param db_path: 数据库文件路径
    """
    def insert_devices_from_txt(txt_file_path, db):
        """
        从 txt 文件读取设备信息并插入数据库。
        :param txt_file_path: txt 文件路径
        :param db: LibraryDatabase 对象
        """
        # 从文件路径提取 location
        location = os.path.basename(txt_file_path)[:-4]  # 提取文件名并去掉 ".txt"
        location = location[:len(location) - 2]  # 去掉倒数两个字符

        with open(txt_file_path, "r", encoding="utf-8") as file:
            lines = file.readlines()

        for line in lines:
            line = line.strip()
            if line:
                parts = line.split(", ")
                devId = parts[0].split(": ")[1]
                devName = parts[1].split(": ")[1]

                # 插入设备数据
                device_data = {
                    "devId": devId,
                    "devName": devName,
                    "location": location
                }
                db.insert_device(device_data)

        print(f"文件 {txt_file_path} 的设备信息已成功插入到数据库，location: {location}")

    # 初始化数据库
    db = LibraryDatabase(db_name=db_path)

    try:
        # 遍历文件夹中的所有 .txt 文件
        for filename in os.listdir(folder_path):
            if filename.endswith(".txt"):
                txt_file_path = os.path.join(folder_path, filename)
                insert_devices_from_txt(txt_file_path, db)

        print("所有设备信息已成功插入到数据库！")
    except Exception as e:
        print(f"处理文件夹 {folder_path} 时出现错误: {e}")
    finally:
        # 查询插入的设备信息验证
        device_info = db.cursor.execute("SELECT * FROM devices").fetchall()
        print("查询所有设备信息:", device_info)

        # 关闭数据库连接
        db.close()

if __name__ == "__main__":
    pass
    # # 文件夹路径，包含多个 .txt 文件
    # folder_path = "座位信息"  # 替换为实际文件夹路径
    #
    # # 数据库路径
    # db_path = "/db/library.db"
    #
    # # 调用函数，将设备信息插入数据库
    # insert_devices_from_folder_to_db(folder_path, db_path)
