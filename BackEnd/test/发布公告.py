# -*- coding: utf-8 -*-
import requests
import json
from datetime import datetime
from config.config import server_ip
# 接口地址
API_URL = f"http://{server_ip}/db/insert_announcement"  # 根据实际服务地址调整

# 准备数据
announcements = [
    {
        "title": "暂无",
        "content": "1.该系统仅供小范围内使用，严禁未经许可的分享\n2.目前支持预约区域有二楼AB区，四楼A区\n3.请检查学号绑定是否正确，否则可能导致预约失败\n4.配置信息后请勿着急，每天上午7:01会自动预约次日的座位\n5.该项目仅由一人独立开发，部分bug在所难免\n6.目前该软件免费使用，云服务器价格为19r/月，作者自掏腰包",
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
