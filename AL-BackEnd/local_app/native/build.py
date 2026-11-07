"""Explicit platform-pinned native builds using existing compatible tools only."""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import sys
import sysconfig
import uuid

from ..models import AppError

CLANG=Path('/Library/Developer/CommandLineTools/usr/bin/clang++')
SDK=Path('/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk')
SOURCES=('executor.cpp','authority.hpp','profile.hpp','peer_threads.hpp','wire.hpp','platform.hpp')


def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def build_target():
    system = 'linux' if sys.platform.startswith('linux') else sys.platform
    machine = platform.machine()
    if system not in ('darwin','linux') or not re.fullmatch(r'[A-Za-z0-9_-]+',machine):
        raise AppError('native_toolchain_missing','当前平台尚未支持原生执行器',503)
    return {'platform':system,'machine':machine}


def build_folder(root, identity, output=None):
    target = build_target()
    return Path(output or (Path(root)/'.runtime/native-executor')) / (
        identity[:24]+'-'+target['platform']+'-'+target['machine'])


LIBRARIES = (('libcurl','curl/curl.h','curl'),
             ('sqlite3','sqlite3.h','sqlite3'),
             ('libcrypto','openssl/sha.h','crypto'))


def installed_library(folder, name):
    candidates = [folder/('lib'+name+'.so')]
    if folder.is_dir():
        candidates.extend(sorted(folder.glob('lib'+name+'.so.*'))[:8])
    return next((path.resolve() for path in candidates if path.is_file()),None)


def prefix_dependencies(prefix):
    prefix = Path(prefix).resolve()
    include = prefix/'include'
    libraries = {}
    for _,header,name in LIBRARIES:
        if not (include/header).is_file():
            return None
        library = next((path for folder in (prefix/'lib',prefix/'lib64')
                        if (path := installed_library(folder,name)) is not None),None)
        if library is None or not library.is_relative_to(prefix):
            return None
        libraries[name] = str(library)
    folders = sorted({str(Path(path).parent) for path in libraries.values()})
    flags = ['-I'+str(include),*libraries.values()]
    for folder in folders:
        flags += ['-Wl,-rpath,'+folder,'-Wl,-rpath-link,'+folder]
    return {'family':'python_prefix','prefix':str(prefix),
            'libraries':libraries,'flags':flags}


def system_dependencies():
    pkg = next((str(path) for path in (Path('/usr/bin/pkg-config'),Path('/bin/pkg-config'))
                if path.is_file()),None)
    if not pkg:
        raise AppError('native_toolchain_missing','缺少一致的现有Linux开发库；未安装任何依赖',503)
    env = os.environ.copy()
    multiarch = sysconfig.get_config_var('MULTIARCH')
    folders = ['/usr/lib/pkgconfig','/usr/share/pkgconfig','/usr/local/lib/pkgconfig','/usr/local/share/pkgconfig']
    if isinstance(multiarch,str) and re.fullmatch(r'[A-Za-z0-9_-]+',multiarch):
        folders.append('/usr/lib/'+multiarch+'/pkgconfig')
    env['PKG_CONFIG_PATH']='';env['PKG_CONFIG_LIBDIR']=os.pathsep.join(folders)
    env.pop('PKG_CONFIG_SYSROOT_DIR',None)
    def variable(package,name):
        result = subprocess.run([pkg,'--variable='+name,package],capture_output=True,
                                text=True,timeout=5,env=env)
        if result.returncode or not 0 < len(result.stdout.strip()) <= 4096:
            raise ValueError('installed package metadata unavailable')
        return Path(result.stdout.strip()).resolve()
    try:
        prefixes = []; libraries = {}; includes = []
        for package,header,name in LIBRARIES:
            prefix = variable(package,'prefix');include = variable(package,'includedir')
            folder = variable(package,'libdir');library = installed_library(folder,name)
            if (prefix not in (Path('/usr'),Path('/usr/local')) or not include.is_relative_to(prefix)
                    or not (include/header).is_file() or library is None or not library.is_relative_to(prefix)):
                raise ValueError('inconsistent system library metadata')
            prefixes.append(prefix);includes.append(include);libraries[name]=str(library)
        if len(set(prefixes)) != 1:
            raise ValueError('mixed system library prefixes')
        flags = ['-I'+str(path) for path in dict.fromkeys(includes)] + list(libraries.values())
        for folder in sorted({str(Path(path).parent) for path in libraries.values()}):
            flags += ['-Wl,-rpath,'+folder,'-Wl,-rpath-link,'+folder]
        return {'family':'system_pkg_config','prefix':str(prefixes[0]),'libraries':libraries,'flags':flags}
    except (OSError,ValueError,subprocess.TimeoutExpired):
        raise AppError('native_toolchain_missing','未找到同一来源的curl/sqlite3/crypto头库；未安装任何依赖',503) from None


def linux_dependencies():
    return prefix_dependencies(Path(sys.prefix)) or system_dependencies()


def compiler_plan(root, temporary):
    common = ['-std=c++17','-O2','-Wall','-Wextra','-Werror']
    source = str(Path(root)/'AL-BackEnd/native_executor/executor.cpp')
    if build_target()['platform'] == 'darwin':
        if not CLANG.is_file() or not SDK.is_dir():
            raise AppError('native_toolchain_missing','缺少兼容的现有Mac编译工具；未安装任何依赖',503)
        return [str(CLANG),*common,'-isysroot',str(SDK),source,'-lcurl','-lsqlite3','-o',str(temporary)],None
    compiler = shutil.which('c++') or shutil.which('g++')
    if not compiler:
        raise AppError('native_toolchain_missing','缺少已安装的Linux C++编译器；未安装任何依赖',503)
    dependencies = linux_dependencies()
    return [compiler,*common,'-pthread','-static-libstdc++','-static-libgcc',source,*dependencies['flags'],'-o',str(temporary)],dependencies


def compiler_command(root, temporary):
    return compiler_plan(root,temporary)[0]


def startup_smoke(executable):
    """No arguments exit before endpoint or ledger creation; exercise the loader."""
    try:
        result = subprocess.run([str(executable)],capture_output=True,text=True,timeout=5)
        return {'exit_code':result.returncode,'stdout':result.stdout[:4096],
                'stderr':result.stderr[:4096]}
    except (OSError,subprocess.TimeoutExpired) as exc:
        return {'exit_code':None,'stdout':'','stderr':'','error_type':type(exc).__name__}


def startup_valid(result):
    return (result.get('exit_code') == 2 and result.get('stdout') == ''
            and result.get('stderr','').startswith('usage: executor socket ')
            and 'workers init|open' in result.get('stderr',''))


def dependency_liveness(executable, dependencies):
    """Inspect the actual loader resolution, including curl's SSL dependency."""
    ldd = shutil.which('ldd')
    if not ldd:
        raise AppError('native_toolchain_missing','缺少现有ldd，未验证Linux链接来源',503)
    result = subprocess.run([ldd,str(executable)],capture_output=True,text=True,timeout=5)
    if result.returncode:
        raise AppError('native_linkage_invalid','Linux动态依赖无法解析，未启用执行器',503)
    actual = {}
    for line in result.stdout.splitlines():
        match = re.match(r'\s*lib(curl|sqlite3|crypto|ssl)\.so\S*\s+=>\s+(\S+)',line)
        if not match:
            continue
        name,path = match.groups()
        if not path.startswith('/'):
            raise AppError('native_linkage_invalid','Linux动态依赖缺失，未启用执行器',503)
        path = Path(path).resolve()
        expected = dependencies['libraries'].get(name)
        if ((expected is not None and path != Path(expected).resolve())
                or (expected is None and not path.is_relative_to(Path(dependencies['prefix'])))):
            raise AppError('native_linkage_invalid','Linux动态依赖来源混用，未启用执行器',503)
        actual[name] = str(path)
    if not {'curl','sqlite3','crypto'} <= actual.keys():
        raise AppError('native_linkage_invalid','Linux动态依赖验证不完整，未启用执行器',503)
    return actual


def private_directory(path):
    path=Path(path)
    aliases={Path('/var'):Path('/private/var'),Path('/tmp'):Path('/private/tmp')}
    if any(parent.is_symlink() and not (parent in aliases and parent.resolve()==aliases[parent])
           for parent in (path,*path.parents)):
        raise AppError('unsafe_native_path','执行器路径不能为符号链接',503)
    path=path.resolve()
    path.mkdir(parents=True,exist_ok=True,mode=0o700)
    info=path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:
        raise AppError('unsafe_native_path','执行器目录须由当前用户私有持有',503)
    return path


@dataclass(frozen=True)
class NativeBuild:
    executable:Path
    source_sha256:str
    binary_sha256:str
    manifest:Path


def source_identity(root):
    folder=Path(root)/'AL-BackEnd/native_executor'
    values={}
    for name in SOURCES:
        path=folder/name
        if path.is_symlink() or not path.is_file():
            raise AppError('native_source_missing','执行器源码缺失，请先修复安装',503)
        values[name]=digest(path)
    identity=hashlib.sha256(json.dumps(values,sort_keys=True).encode()).hexdigest()
    return identity,values


def read_build(root,output=None):
    identity,sources=source_identity(root)
    folder=build_folder(root,identity,output)
    executable,manifest=folder/'executor',folder/'build.json'
    try:
        for path in (executable,manifest):
            info=path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid()
                    or info.st_mode&0o022 or info.st_nlink!=1):raise ValueError()
        value=json.loads(manifest.read_text())
        if (value['target']!=build_target() or value['source_sha256']!=identity or value['sources']!=sources
                or value['binary_sha256']!=digest(executable)
                or not os.access(executable,os.X_OK)):raise ValueError()
        if build_target()['platform'] == 'linux':
            if dependency_liveness(executable,value['dependencies']) != value['linked_libraries']:
                raise ValueError('retained dynamic dependency identity changed')
            if not startup_valid(startup_smoke(executable)):
                raise ValueError('retained binary cannot start')
    except (OSError,ValueError,KeyError,TypeError):
        raise AppError('native_build_missing','执行器编译文件尚未通过校验，请先完成构建',503) from None
    return NativeBuild(executable,identity,value['binary_sha256'],manifest)


def build_native(root,output=None):
    root=Path(root);identity,sources=source_identity(root)
    try:return read_build(root,output)
    except AppError:pass
    target=build_target()
    parent=private_directory(output or root/'.runtime/native-executor')
    folder=private_directory(build_folder(root,identity,parent))
    executable,manifest=folder/'executor',folder/'build.json'
    if executable.exists() or manifest.exists():
        raise AppError('native_build_conflict','已有编译身份不匹配，保留文件供核对',503)
    temporary=folder/('executor-'+uuid.uuid4().hex+'.part')
    command,dependencies=compiler_plan(root,temporary)
    try:
        result=subprocess.run(command,capture_output=True,text=True,timeout=60)
        log={'argv':command,'exit_code':result.returncode,'stdout':result.stdout,'stderr':result.stderr,
             'source_sha256':identity,'sources':sources,'target':target,'dependencies':dependencies}
        (folder/'compile.json').write_text(json.dumps(log,indent=2)+'\n')
        if result.returncode:raise AppError('native_build_failed','执行器编译失败，请检查编译日志',503)
        linked = dependency_liveness(temporary,dependencies) if dependencies else None
        log['linked_libraries'] = linked
        smoke = startup_smoke(temporary) if dependencies else None
        log['startup_smoke'] = smoke
        (folder/'compile.json').write_text(json.dumps(log,indent=2)+'\n')
        if dependencies and not startup_valid(smoke):
            raise AppError('native_linkage_invalid','Linux执行器无法启动，请检查编译诊断',503)
        os.chmod(temporary,0o700)
        value={'source_sha256':identity,'sources':sources,'binary_sha256':digest(temporary),
               'target':target,'dependencies':dependencies,'linked_libraries':linked,'startup_smoke':smoke}
        with temporary.open('rb') as stream:os.fsync(stream.fileno())
        temporary.rename(executable)
        fd = os.open(manifest,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'w') as stream:
            json.dump(value,stream,indent=2);stream.write('\n');stream.flush();os.fsync(stream.fileno())
        return read_build(root,output)
    finally:
        if temporary.exists() and not temporary.is_symlink():temporary.unlink()
