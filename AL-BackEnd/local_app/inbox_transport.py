"""Project-scoped inbox routing with original HTTPS identity and no POST retry."""
import ipaddress
import json
import os
from pathlib import Path
import stat
from urllib.parse import urlsplit

from requests.adapters import HTTPAdapter
from requests.exceptions import InvalidURL, SSLError
from requests.utils import select_proxy

from .models import AppError


def _origin(value):
    if not isinstance(value,str):raise ValueError('Invalid inbox origin')
    parsed=urlsplit(value)
    if (parsed.scheme!='https' or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.path or parsed.query or parsed.fragment):
        raise ValueError('Invalid inbox origin')
    return parsed.hostname,parsed.port or 443,parsed.netloc


def read_route(data_dir,origin):
    path=Path(data_dir)/'inbox/route.json'
    try:info=path.lstat()
    except FileNotFoundError:return None
    if (path.parent.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid()
            or info.st_mode&0o077 or info.st_nlink!=1 or info.st_size>4096):
        raise AppError('unsafe_inbox_route','收件连接配置须为私有普通文件',503)
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        opened=os.fstat(stream.fileno())
        if (opened.st_dev,opened.st_ino)!=(info.st_dev,info.st_ino):
            raise AppError('unsafe_inbox_route','收件连接配置已变化',503)
        try:value=json.load(stream)
        except RecursionError:raise ValueError('Invalid inbox route nesting') from None
    if (not isinstance(value,dict) or set(value)!={'schema','origin','connect_ip'}
            or type(value['schema']) is not int or value['schema']!=1 or value['origin']!=origin
            or not isinstance(value['connect_ip'],str)):
        raise ValueError('Invalid inbox route')
    _origin(origin)
    address=ipaddress.ip_address(value['connect_ip'])
    if not address.is_global:raise ValueError('Inbox route requires a public IP')
    return {'origin':origin,'connect_ip':str(address)}


class OriginIPAdapter(HTTPAdapter):
    """Only the socket address changes; URL, Host, SNI and CA verification remain."""
    def __init__(self,origin,connect_ip):
        self.hostname,self.port,self.authority=_origin(origin)
        self.connect_ip=str(ipaddress.ip_address(connect_ip))
        super().__init__(max_retries=0)

    def _check(self,request,verify=True,proxies=None):
        target=urlsplit(request.url)
        if (target.scheme!='https' or target.hostname!=self.hostname
                or (target.port or 443)!=self.port or target.username is not None
                or target.password is not None):
            raise InvalidURL('Inbox route origin mismatch',request=request)
        if verify is False:raise SSLError('Inbox route requires TLS verification',request=request)
        if select_proxy(request.url,proxies):raise InvalidURL('Inbox route does not use a proxy',request=request)

    def get_connection_with_tls_context(self,request,verify,proxies=None,cert=None):
        self._check(request,verify,proxies)
        host,pool=self.build_connection_pool_key_attributes(request,verify,cert)
        host['host']=self.connect_ip
        pool['server_hostname']=self.hostname
        pool['assert_hostname']=self.hostname
        return self.poolmanager.connection_from_host(**host,pool_kwargs=pool)

    def add_headers(self,request,**kwargs):
        self._check(request)
        request.headers['Host']=self.authority


def configure_session(session,origin,route):
    if route is None:return
    if route['origin']!=origin:raise ValueError('Inbox route origin mismatch')
    session.mount(origin+'/',OriginIPAdapter(origin,route['connect_ip']))
