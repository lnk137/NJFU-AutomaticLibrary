# -*- coding: utf-8 -*-
import requests
import json
from datetime import datetime
from utils import config
# 接口地址
API_URL = f"http://{config.SERVER_IP}/db/insert_announcement"  # 根据实际服务地址调整

# 准备数据
announcements = [
    {
        "title": "v2.0.2更新",
        "content":
        '''
        https://wwoc.lanzoup.com/i65qg2q9avxc
        密码:6ebv
        ''',
        "importance": "高",
        "publish_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    },
]

try:
    for announcement in announcements:
        # 发送 POST 请求
        response = requests.post(
            API_URL,
            headers={"Content-Type": "application/json"},
            data=json.dumps(announcement)
        )

        # 解析响应
        if response.status_code == 200:
            print(f"公告插入成功！标题：{announcement['title']}")
            print("响应数据:", response.json())
        else:
            print(f"请求失败，状态码: {response.status_code}")
            print("错误信息:", response.json())
except requests.exceptions.RequestException as e:
    print("请求失败:", e)
