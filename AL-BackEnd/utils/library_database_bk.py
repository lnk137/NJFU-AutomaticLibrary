from datetime import datetime
from utils import config
import sqlite3
import json
import os

db_dir = os.path.dirname(config.DB_NAME)
if not os.path.exists(db_dir):
    os.makedirs(db_dir)

class LibraryDatabase:
    def __init__(self, db_name=config.DB_NAME):
        """
        初始化数据库连接，并创建必要的表格。

        :param db_name: 数据库文件路径，默认为 "../db/library.db"
        """
        self.db_name = db_name
        self.conn = sqlite3.connect(self.db_name)
        self.cursor = self.conn.cursor()
        self._create_tables()

    def _create_tables(self):
        """
        创建数据库中的所有必要表格。

        创建表格：
        - user_info: 存储用户信息
        - reservation_info: 存储预约信息
        - devices: 存储设备信息
        - reservation_result: 存储预约结果
        - announcement_info: 存储公告信息
        """
        # 创建 user_info 表
        create_user_table_query = """
        CREATE TABLE IF NOT EXISTS user_info (
            pid TEXT PRIMARY KEY,  -- 使用 pid 作为主键
            uuid TEXT,
            accNo INTEGER,
            logonName TEXT,
            trueName TEXT,
            className TEXT,
            sex INTEGER,
            deptName TEXT,
            token TEXT
        );
        """
        self.cursor.execute(create_user_table_query)

        # 创建 reservation_info 表
        create_reservation_table_query = """
        CREATE TABLE IF NOT EXISTS reservation_info (
            pid TEXT PRIMARY KEY,  -- 使用 pid 作为主键
            begin_time TEXT,
            end_time TEXT,
            seat_list TEXT,  -- 存储为 JSON 字符串格式
            logonName TEXT,
            password TEXT,
            priority INTEGER DEFAULT 0,  -- 默认为 0
            is_reserved BOOLEAN DEFAULT TRUE  -- 是否正在预约中，默认值为 TRUE
        );
        """
        self.cursor.execute(create_reservation_table_query)

        # 创建 devices 表
        create_devices_table_query = """
        CREATE TABLE IF NOT EXISTS devices (
            devId TEXT PRIMARY KEY,  -- 设备 ID 主键
            devName TEXT,           -- 设备名称
            location TEXT           -- 设备位置
        );
        """
        self.cursor.execute(create_devices_table_query)

        self.conn.commit()

        # 创建 reservation_result 表
        create_reservation_result_table_query = """
        CREATE TABLE IF NOT EXISTS reservation_result (
            pid TEXT PRIMARY KEY,
            result_info TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (pid) REFERENCES user_info (pid)
        );
        """
        self.cursor.execute(create_reservation_result_table_query)

        # 创建 announcement_info 表
        create_announcement_query = """
        CREATE TABLE IF NOT EXISTS announcement_info (
            id INTEGER PRIMARY KEY AUTOINCREMENT,  -- 自增主键
            title TEXT NOT NULL,                   -- 公告标题
            content TEXT NOT NULL,                 -- 公告内容
            publish_time TEXT NOT NULL,            -- 发布时间
            importance TEXT DEFAULT '中',          -- 重要程度 (高, 中, 低)
            update_time TEXT                       -- 更新时间
        );
        """
        self.cursor.execute(create_announcement_query)

        self.conn.commit()

    def insert_user(self, user_info):
        """
        插入用户数据。如果主键冲突则忽略。

        :param user_info: 用户数据，字典格式，包含字段：
                          uuid, accNo, pid, logonName, trueName, className, sex, deptName, token
        :return: None
        """
        insert_query = """
           INSERT INTO user_info (uuid, accNo, pid, logonName, trueName, className, sex, deptName, token)
           VALUES (:uuid, :accNo, :pid, :logonName, :trueName, :className, :sex, :deptName, :token)
           ON CONFLICT(pid) DO NOTHING;
           """
        self.cursor.execute(insert_query, user_info)
        self.conn.commit()

    def insert_or_update_reservation(self, reservation_data):
        """
        插入或更新预约数据。

        :param reservation_data: 预约数据，字典格式，包含字段：
                                 pid, begin_time, end_time, seat_list, logonName, password, is_reserved
        :return: None
        """
        try:
            insert_on_conflict_query = """
            INSERT INTO reservation_info (pid, begin_time, end_time, seat_list, logonName, password, is_reserved)
            VALUES (:pid, :begin_time, :end_time, :seat_list, :logonName, :password, :is_reserved)
            ON CONFLICT(pid) DO UPDATE SET
                begin_time = excluded.begin_time,
                end_time = excluded.end_time,
                seat_list = excluded.seat_list,
                logonName = excluded.logonName,
                password = excluded.password,
                is_reserved = excluded.is_reserved;
            """
            # 执行插入或更新
            self.cursor.execute(insert_on_conflict_query, reservation_data)
            self.conn.commit()
            print("数据插入或更新成功！")
        except Exception as e:
            print(f"插入或更新失败: {e}")
            raise

    def insert_device(self, device_data):
        """
        插入设备信息。

        :param device_data: 设备数据，字典格式，包含字段：
                            devId, devName, location
        :return: None
        """
        insert_query = """
        INSERT INTO devices (devId, devName, location)
        VALUES (:devId, :devName, :location)
        ON CONFLICT(devId) DO NOTHING;
        """
        self.cursor.execute(insert_query, device_data)
        self.conn.commit()

    def insert_or_update_reservation_result(self, pid, result_info):
        """
        插入或更新预约结果信息，并自动更新时间戳。

        :param pid: 用户的主键
        :param result_info: 预约结果信息，必须为 JSON 可序列化的格式（字符串）
        :return: None
        """
        try:
            # 确保 result_info 是字符串
            if not isinstance(result_info, str):
                result_info = json.dumps(result_info)

            # 确保 pid 是字符串
            pid = str(pid)

            # 获取本地时间
            local_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

            # 插入或更新数据，使用本地时间
            insert_query = """
            INSERT INTO reservation_result (pid, result_info, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(pid) DO UPDATE SET 
                result_info = excluded.result_info,
                created_at = ?;
            """
            self.cursor.execute(insert_query, (pid, result_info, local_time, local_time))
            self.conn.commit()
        except Exception as e:
            print(f"插入或更新预约结果失败: {e}")
            raise

    def insert_announcement(self, announcement_data):
        """
        插入公告信息（自动生成 id）。

        :param announcement_data: 公告数据，字典格式，包含字段：
                                  title, content, publish_time, importance, update_time
        :return: None
        """
        try:
            insert_query = """
            INSERT INTO announcement_info (title, content, publish_time, importance, update_time)
            VALUES (:title, :content, :publish_time, :importance, :update_time);
            """
            self.cursor.execute(insert_query, announcement_data)
            self.conn.commit()
            print("公告插入成功！")
        except Exception as e:
            print(f"公告插入失败: {e}")
            raise

    def get_reservations_by_pid(self, pid):
        """
        根据 pid 查询预约信息。

        :param pid: 用户的主键
        :return: 预约信息列表，若无结果返回 None
        """
        query = "SELECT * FROM reservation_info WHERE pid = ?;"
        self.cursor.execute(query, (pid,))
        rows = self.cursor.fetchall()
        if rows:
            columns = [desc[0] for desc in self.cursor.description]
            return [dict(zip(columns, row)) for row in rows]
        return None

    def get_all_active_reservations(self):
        """
        查询所有 is_reserved = 1 的预约记录。

        :return: 正在预约中的记录列表(元素为字典)，若无结果返回空列表
        """
        try:
            query = """
            SELECT 
                pid,
                logonName, 
                password, 
                seat_list, 
                begin_time, 
                end_time, 
                priority
            FROM 
                reservation_info
            WHERE 
                is_reserved = 1;  -- 使用 1 替代 TRUE
            """
            self.cursor.execute(query)
            rows = self.cursor.fetchall()

            # 如果有结果，将字段名与值组合为字典返回
            if rows:
                columns = [desc[0] for desc in self.cursor.description]
                return [dict(zip(columns, row)) for row in rows]
            return []  # 如果没有结果，返回空列表
        except Exception as e:
            print(f"查询失败: {e}")
            raise

    def get_device_id_by_name(self, devName):
        """
        根据设备名称查询设备 ID。

        :param devName: 设备名称
        :return: 设备 ID 或 None
        """
        query = "SELECT devId FROM devices WHERE devName = ?;"
        self.cursor.execute(query, (devName,))
        result = self.cursor.fetchone()
        return result[0] if result else None

    def get_reservation_result_by_pid(self, pid):
        """
        根据 pid 查询预约结果信息和创建时间。

        :param pid: 用户的主键
        :return: 包含预约结果信息和时间的字典，或 None
        """
        query = "SELECT result_info, created_at FROM reservation_result WHERE pid = ?;"
        self.cursor.execute(query, (pid,))
        result = self.cursor.fetchone()
        if result:
            return {"result_info": result[0], "created_at": result[1]}
        return None

    def get_announcements(self, importance=None):
        """
        查询公告信息。

        :param importance: 可选，按重要程度过滤 (高, 中, 低)
        :return: 公告列表，若无结果返回空列表
        """
        try:
            if importance:
                query = "SELECT * FROM announcement_info WHERE importance = ? ORDER BY publish_time DESC;"
                self.cursor.execute(query, (importance,))
            else:
                query = "SELECT * FROM announcement_info ORDER BY publish_time DESC;"
                self.cursor.execute(query)

            rows = self.cursor.fetchall()
            columns = [desc[0] for desc in self.cursor.description]
            return [dict(zip(columns, row)) for row in rows]
        except Exception as e:
            print(f"查询公告信息失败: {e}")
            return []

    def update_reservation_status(self, pid, is_reserved):
        """
        根据 pid 更新预约状态。

        :param pid: 用户的主键
        :param is_reserved: 预约状态 (True/False)
        :return: None
        """
        update_query = """
        UPDATE reservation_info
        SET is_reserved = ?
        WHERE pid = ?;
        """
        self.cursor.execute(update_query, (is_reserved, pid))
        self.conn.commit()

    def update_reservation(self, reservation_data):
        """
        更新预约信息。

        :param reservation_data: 预约数据，字典格式，包含字段：
                                 pid, begin_time, end_time, seat_list, logonName, password, is_reserved
        :return: None
        """
        try:
            # 构造更新 SQL
            update_query = """
            UPDATE reservation_info
            SET begin_time = :begin_time,
                end_time = :end_time,
                seat_list = :seat_list,
                logonName = :logonName,
                password = :password,
                is_reserved = :is_reserved
            WHERE pid = :pid
            """
            # 执行更新操作
            self.cursor.execute(update_query, reservation_data)
            self.conn.commit()
            print("数据更新成功！")
        except Exception as e:
            print(f"更新失败: {e}")
            raise

    def update_priority_by_pid(self, pid, priority):
        """
        根据 pid 更新优先级。

        :param pid: 用户的主键
        :param priority: 更新后的优先级值
        :return: None
        """
        update_query = """
        UPDATE reservation_info
        SET priority = ?
        WHERE pid = ?;
        """
        self.cursor.execute(update_query, (priority, pid))
        self.conn.commit()

    def close(self):
        """
        关闭数据库连接。

        :return: None
        """
        self.conn.close()


# 使用示例
if __name__ == "__main__":
    # 初始化数据库
    db = LibraryDatabase()

    # 关闭数据库连接
    db.close()
