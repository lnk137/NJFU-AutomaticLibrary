import base64
import random
import string

from Crypto.Util.Padding import pad
from Crypto.Cipher import AES
from Crypto.Cipher import PKCS1_v1_5
from Crypto.PublicKey import RSA

class PasswordEncryptor:
    @staticmethod
    def aes_encrypt_password(salt, password):
        """
        使用随机前缀和 IV 对密码进行加密。

        :param salt: 加密盐值
        :param password: 原始密码
        :return: 加密后的密码字符串
        """
        try:
            random_prefix = ''.join(random.choice(string.ascii_letters + string.digits) for _ in range(64))
            random_iv = ''.join(random.choice(string.ascii_letters + string.digits) for _ in range(16))
            cipher = AES.new(salt.encode('utf-8'), AES.MODE_CBC, random_iv.encode('utf-8'))
            encrypted_password = base64.b64encode(
                cipher.encrypt(pad((random_prefix + password).encode('utf-8'), AES.block_size))).decode('utf-8')
            return encrypted_password
        except Exception as e:
            print("加密密码时发生异常:", str(e))
            return None

    @staticmethod
    def set_public_key(public_key):
        """
        将公钥字符串格式化为 PEM 格式，并导入为 RSA 公钥。

        :param public_key: 公钥字符串
        :return: RSA 公钥对象
        """
        pem = "-----BEGIN PUBLIC KEY-----\n"
        pem += "\n".join(public_key[i:i + 64] for i in range(0, len(public_key), 64))
        pem += "\n-----END PUBLIC KEY-----"
        return RSA.importKey(pem)

    @staticmethod
    def encrypt_with_public_key(public_key, text):
        """
        使用 RSA 公钥加密文本。

        :param public_key: RSA 公钥对象
        :param text: 需要加密的文本
        :return: 加密后的文本（base64 编码字符串）
        """
        text_bytes = text.encode('utf-8')
        cipher = PKCS1_v1_5.new(public_key)
        encrypted = cipher.encrypt(text_bytes)
        return base64.b64encode(encrypted).decode('utf-8')