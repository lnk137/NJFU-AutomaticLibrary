"""Explicit legacy cancellation utility; no account or receipt is preconfigured."""
import os


def load_test_config():
    """Require explicit credentials; no embedded account or prior receipt."""
    required = {'pid': 'NJFU_LEGACY_STUDENT_ID', 'lib_password': 'NJFU_LEGACY_LIBRARY_PASSWORD',
                'vpn_password': 'NJFU_LEGACY_VPN_PASSWORD', 'record_id': 'NJFU_LEGACY_CANCEL_RECORD_ID'}
    values = {field: os.environ.get(name) for field, name in required.items()}
    if any(not value for value in values.values()):
        raise RuntimeError("Required legacy test environment variables are missing")
    return values


def reservation(res_item):
    uuid = res_item["record_id"]
    from utils.vpn_system import VPNSystem
    from utils.library_system import LibrarySystem
    import requests
    #1.加载账号、密码、座位信息
    pid = res_item["pid"]
    vpn_password = res_item["vpn_password"]
    lib_password = res_item["lib_password"]
    # 如果末尾是全角“！”，就替换成半角“!”
    if lib_password.endswith('！'):
        lib_password = lib_password[:-1] + '!'

    #同步session
    shared_session = requests.Session()
    vpn = VPNSystem(pid, vpn_password)
    library = LibrarySystem(pid, lib_password)

    vpn.session = library.session = shared_session


    if not vpn.vpn_login():
        print(f"VPN 登录失败，无法继续预约")
        return
    library.library_login()
    library.delete_seat(uuid)


if __name__ == "__main__":
    reservation(load_test_config())
