from datetime import datetime, timedelta

# 测试数据
seat_dict = {
    'target_time': "2025-05-12 12:00:00-16:00:00"
}

# 拆解 target_time
target_time = seat_dict['target_time']
date_str, time_range = target_time.split(' ')
begin_time_str, end_time_str = time_range.split('-')

# 拼接成完整的datetime字符串
begin_time = datetime.strptime(f"{date_str} {begin_time_str}", "%Y-%m-%d %H:%M:%S")
end_time = datetime.strptime(f"{date_str} {end_time_str}", "%Y-%m-%d %H:%M:%S")

# 只变小时：起始时间加1小时
new_begin = begin_time + timedelta(hours=1)

# 计算时长（小时数）
duration = (end_time - new_begin).total_seconds() / 3600

# 如果原时长小于2小时，结束时间也加1小时，否则保持不变
if duration < 2:
    new_end = end_time + timedelta(hours=1)
else:
    new_end = end_time

# 日期部分不变，只输出新的小时
new_begin_str = f"{date_str} {new_begin.strftime('%H:%M:%S')}"
new_end_str = f"{date_str} {new_end.strftime('%H:%M:%S')}"

print("原始起始时间:", begin_time.strftime("%Y-%m-%d %H:%M:%S"))
print("原始结束时间:", end_time.strftime("%Y-%m-%d %H:%M:%S"))
print("新的起始时间:", new_begin_str)
print("新的结束时间:", new_end_str)
