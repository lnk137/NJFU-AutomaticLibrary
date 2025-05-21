# -*- coding: utf-8 -*-
import requests
import json
from datetime import datetime
from utils import config
# API_URL = f"http://{config.SERVER_IP}/db/announcement"
# 接口地址
API_URL = f"http://127.0.0.1:5001/db/announcement"  # 直接使用完整的URL

# 准备数据
announcements = [
    {
        "title": "使用必看(25.5.21更新)",
        "content": """
教程地址:http://view.lnk137.xyz/
        """.strip(),
        "importance": "高",
        "publish_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    },
]

def publish_announcement(announcement):
    """发布单个公告"""
    try:
        print(f"📡 正在发送请求到: {API_URL}")
        response = requests.post(
            API_URL,
            headers={"Content-Type": "application/json"},
            data=json.dumps(announcement)
        )
        
        if response.status_code == 200:
            print(f"✅ 公告发布成功！标题：{announcement['title']}")
            print(f"📝 响应数据: {response.json()}")
            return True
        else:
            print(f"❌ 请求失败，状态码: {response.status_code}")
            print(f"❌ 错误信息: {response.text}")
            return False
    except requests.exceptions.RequestException as e:
        print(f"❌ 请求异常: {e}")
        return False

def main():
    """主函数"""
    print("🚀 开始发布公告...")
    success_count = 0
    
    for announcement in announcements:
        if publish_announcement(announcement):
            success_count += 1
    
    print(f"\n📊 发布结果统计:")
    print(f"✅ 成功: {success_count}")
    print(f"❌ 失败: {len(announcements) - success_count}")

if __name__ == "__main__":
    main()
