import re

# 定义一个函数提取 devId 和 devName
def extract_dev_pairs(file_path):
    # 定义匹配 devId 和 devName 的正则表达式
    dev_id_pattern = r'"devSn":\s*(\d+)'
    dev_name_pattern = r'"devName":\s*"([^"]+)"'

    # 打开并读取文件
    with open(file_path, 'r', encoding='utf-8') as file:
        content = file.read()

        # 查找所有 devId 和 devName
        dev_ids = re.findall(dev_id_pattern, content)
        dev_names = re.findall(dev_name_pattern, content)

        # 两两配对
        dev_pairs = list(zip(dev_ids, dev_names))

    return dev_pairs

# 保存结果到文件的函数
def save_dev_pairs_to_file(dev_pairs, output_file):
    with open(output_file, 'w', encoding='utf-8') as file:
        for dev_id, dev_name in dev_pairs:
            file.write(f"devId: {dev_id}, devName: {dev_name}\n")

# 示例使用
file_path = "2A原始.txt"  # 替换为你的文件路径
output_file = "2A座位.txt"  # 输出文件名

result = extract_dev_pairs(file_path)
save_dev_pairs_to_file(result, output_file)

print(f"提取的 devId 和 devName 已保存到 {output_file}")