"""
图书馆预约查询测试模块

本模块用于测试图书馆预约查询功能，包括：
1. 自动登录VPN和图书馆系统
2. 查询预约信息
3. 格式化输出查询结果

使用方法：
python 查询预约测试.py
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List, Tuple

from utils.library_system import LibrarySystem
from utils import config

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

def format_reservation_info(reservations: List[Dict[str, Any]]) -> str:
    """
    格式化预约信息输出
    
    Args:
        reservations: 预约信息列表
        
    Returns:
        str: 格式化后的预约信息
    """
    if not reservations:
        return "无预约记录"
        
    output = [f"\n查询结果 ({len(reservations)} 条记录):"]
    output.append("-" * 50)
    
    for resv in reservations:
        output.extend([
            f"预约ID: {resv['uuid']}",
            f"预约人: {resv['resvName']}",
            f"预约时间: {resv['resvBeginTime']} - {resv['resvEndTime']}",
            f"座位信息: {resv['devInfo']['roomName']} - {resv['devInfo']['devName']}",
            f"预约状态: {resv['resvStatus']}",
            "-" * 50
        ])
    
    return "\n".join(output)

def query_reservation(user_config: Dict[str, Any]) -> None:
    """
    查询用户预约信息
    
    完整的查询流程：
    1. 初始化图书馆系统（包含VPN登录）
    2. 查询预约信息
    3. 格式化并输出结果
    
    Args:
        user_config: 用户配置信息，包含：
            - pid: 学号
            - vpn_password: VPN密码
            - lib_password: 图书馆密码
    """
    try:
        # 加载账号信息
        pid = user_config["pid"]
        vpn_password = user_config["vpn_password"]
        lib_password = user_config["lib_password"].replace('！', '!')  # 处理全角感叹号
        
        logger.info(f"开始查询用户 {pid} 的预约信息")
        
        try:
            # 初始化图书馆系统（包含VPN登录）
            library = LibrarySystem(
                username=pid,
                password=lib_password,
                vpn_password=vpn_password
            )
            
            # 查询预约信息
            reservations, message = library.get_reservation_info()
            
            if reservations is None:
                logger.error(f"查询失败: {message}")
                return
                
            # 输出查询结果
            logger.info(format_reservation_info(reservations))
            
        except Exception as e:
            logger.error(f"查询过程发生异常: {str(e)}")
            
    except Exception as e:
        logger.error(f"程序运行出错: {str(e)}")

def main():
    """主函数"""
    # 测试用户配置
    test_user = {
        "pid": "2210104120",
        "is_reserved": "True",
        "lib_password": "njfu160101!",
        "vpn_password": "AAa040328/",
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
    
    try:
        query_reservation(test_user)
    except KeyboardInterrupt:
        logger.info("程序被用户中断")
    except Exception as e:
        logger.error(f"程序运行出错: {str(e)}")
    finally:
        logger.info("程序已退出")

if __name__ == "__main__":
    main()
