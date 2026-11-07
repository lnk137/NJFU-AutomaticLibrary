#pragma once
#include <sqlite3.h>
#include <atomic>
#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <string>
#include <stdexcept>

inline uint64_t now_ns();
struct Options {
    std::string variant="wal-raw-t",journal="WAL";
    bool prepared=false;
    unsigned loops=0;
    void load(){variant="wal-raw-t";journal="WAL";prepared=false;loops=0;}
};
inline Options options;
struct SqlProfile {
    uint64_t db_wait_ns=0,begin_ns=0,updates_ns=0,commit_ns=0;
    uint64_t prepare_ns=0,bind_ns=0,step_ns=0,prepare_calls=0;
    uint64_t sync_ns=0,sync_calls=0,sync_flags=0,directory_sync_ns=0,write_ns=0,write_calls=0;
};
inline thread_local SqlProfile *active_sql_profile=nullptr;
struct ProfileScope {
    SqlProfile *prior;
    explicit ProfileScope(SqlProfile &value):prior(active_sql_profile){active_sql_profile=&value;}
    ~ProfileScope(){active_sql_profile=prior;}
};

// Pass-through VFS: delegate every operation to the existing platform VFS.
// Timings surround its real xSync and sync-directory xDelete calls, with identical flags.
namespace timed_vfs {
inline std::atomic<bool> fail_next_profiled_sync{false};
struct alignas(std::max_align_t) File {sqlite3_file base;sqlite3_file *real;};
inline File *file(sqlite3_file *f){return reinterpret_cast<File *>(f);}
inline sqlite3_vfs *parent(sqlite3_vfs *v){return static_cast<sqlite3_vfs *>(v->pAppData);}
inline int close(sqlite3_file *f){return file(f)->real->pMethods->xClose(file(f)->real);}
inline int read(sqlite3_file *f,void *p,int n,sqlite3_int64 o){auto r=file(f)->real;return r->pMethods->xRead(r,p,n,o);}
inline int write(sqlite3_file *f,const void *p,int n,sqlite3_int64 o){auto r=file(f)->real;uint64_t start=now_ns();int rc=r->pMethods->xWrite(r,p,n,o);if(active_sql_profile){active_sql_profile->write_ns+=now_ns()-start;++active_sql_profile->write_calls;}return rc;}
inline int truncate(sqlite3_file *f,sqlite3_int64 n){auto r=file(f)->real;return r->pMethods->xTruncate(r,n);}
inline int sync(sqlite3_file *f,int flags){auto r=file(f)->real;uint64_t start=now_ns();int rc=active_sql_profile && fail_next_profiled_sync.exchange(false)?SQLITE_IOERR_FSYNC:r->pMethods->xSync(r,flags);if(active_sql_profile){active_sql_profile->sync_ns+=now_ns()-start;++active_sql_profile->sync_calls;active_sql_profile->sync_flags|=static_cast<uint64_t>(flags);}return rc;}
inline int size(sqlite3_file *f,sqlite3_int64 *n){auto r=file(f)->real;return r->pMethods->xFileSize(r,n);}
inline int lock(sqlite3_file *f,int n){auto r=file(f)->real;return r->pMethods->xLock(r,n);}
inline int unlock(sqlite3_file *f,int n){auto r=file(f)->real;return r->pMethods->xUnlock(r,n);}
inline int reserved(sqlite3_file *f,int *n){auto r=file(f)->real;return r->pMethods->xCheckReservedLock(r,n);}
inline int control(sqlite3_file *f,int op,void *p){auto r=file(f)->real;return r->pMethods->xFileControl(r,op,p);}
inline int sector(sqlite3_file *f){auto r=file(f)->real;return r->pMethods->xSectorSize(r);}
inline int characteristics(sqlite3_file *f){auto r=file(f)->real;return r->pMethods->xDeviceCharacteristics(r);}
inline int shm_map(sqlite3_file *f,int a,int b,int c,void volatile **p){auto r=file(f)->real;return r->pMethods->xShmMap(r,a,b,c,p);}
inline int shm_lock(sqlite3_file *f,int a,int b,int c){auto r=file(f)->real;return r->pMethods->xShmLock(r,a,b,c);}
inline void shm_barrier(sqlite3_file *f){auto r=file(f)->real;r->pMethods->xShmBarrier(r);}
inline int shm_unmap(sqlite3_file *f,int n){auto r=file(f)->real;return r->pMethods->xShmUnmap(r,n);}
inline int fetch(sqlite3_file *f,sqlite3_int64 a,int b,void **p){auto r=file(f)->real;if(r->pMethods->iVersion<3 || !r->pMethods->xFetch){*p=nullptr;return SQLITE_OK;}return r->pMethods->xFetch(r,a,b,p);}
inline int unfetch(sqlite3_file *f,sqlite3_int64 a,void *p){auto r=file(f)->real;return r->pMethods->iVersion<3 || !r->pMethods->xUnfetch?SQLITE_OK:r->pMethods->xUnfetch(r,a,p);}
inline const sqlite3_io_methods methods={3,close,read,write,truncate,sync,size,lock,unlock,reserved,control,sector,characteristics,shm_map,shm_lock,shm_barrier,shm_unmap,fetch,unfetch};
inline sqlite3_vfs vfs{};
inline void install(){
    sqlite3_vfs *original=sqlite3_vfs_find(nullptr);if(!original)throw std::runtime_error("no SQLite VFS");
    vfs=*original;vfs.zName="njfu-phase-timing";vfs.pNext=nullptr;vfs.pAppData=original;vfs.szOsFile=static_cast<int>(sizeof(File))+original->szOsFile;
    vfs.xOpen=[](sqlite3_vfs *v,const char *name,sqlite3_file *raw,int flags,int *out){auto f=file(raw);f->real=reinterpret_cast<sqlite3_file *>(reinterpret_cast<char *>(raw)+sizeof(File));auto p=parent(v);int rc=p->xOpen(p,name,f->real,flags,out);if(rc==SQLITE_OK)f->base.pMethods=&methods;return rc;};
    vfs.xDelete=[](sqlite3_vfs *v,const char *name,int dir){auto p=parent(v);uint64_t start=now_ns();int rc=p->xDelete(p,name,dir);if(active_sql_profile && dir)active_sql_profile->directory_sync_ns+=now_ns()-start;return rc;};
    vfs.xAccess=[](sqlite3_vfs *v,const char *name,int flags,int *out){auto p=parent(v);return p->xAccess(p,name,flags,out);};
    vfs.xFullPathname=[](sqlite3_vfs *v,const char *name,int n,char *out){auto p=parent(v);return p->xFullPathname(p,name,n,out);};
    vfs.xDlOpen=[](sqlite3_vfs *v,const char *name){auto p=parent(v);return p->xDlOpen(p,name);};
    vfs.xDlError=[](sqlite3_vfs *v,int n,char *out){auto p=parent(v);p->xDlError(p,n,out);};
    vfs.xDlSym=[](sqlite3_vfs *v,void *h,const char *name){auto p=parent(v);return p->xDlSym(p,h,name);};
    vfs.xDlClose=[](sqlite3_vfs *v,void *h){auto p=parent(v);p->xDlClose(p,h);};
    vfs.xRandomness=[](sqlite3_vfs *v,int n,char *out){auto p=parent(v);return p->xRandomness(p,n,out);};
    vfs.xSleep=[](sqlite3_vfs *v,int n){auto p=parent(v);return p->xSleep(p,n);};
    vfs.xCurrentTime=[](sqlite3_vfs *v,double *out){auto p=parent(v);return p->xCurrentTime(p,out);};
    vfs.xGetLastError=[](sqlite3_vfs *v,int n,char *out){auto p=parent(v);return p->xGetLastError(p,n,out);};
    if(vfs.iVersion>=2)vfs.xCurrentTimeInt64=[](sqlite3_vfs *v,sqlite3_int64 *out){auto p=parent(v);return p->xCurrentTimeInt64(p,out);};
    if(vfs.iVersion>=3){
        vfs.xSetSystemCall=[](sqlite3_vfs *v,const char *n,sqlite3_syscall_ptr f){auto p=parent(v);return p->xSetSystemCall(p,n,f);};
        vfs.xGetSystemCall=[](sqlite3_vfs *v,const char *n){auto p=parent(v);return p->xGetSystemCall(p,n);};
        vfs.xNextSystemCall=[](sqlite3_vfs *v,const char *n){auto p=parent(v);return p->xNextSystemCall(p,n);};
    }
    if(sqlite3_vfs_register(&vfs,0)!=SQLITE_OK)throw std::runtime_error("timing VFS registration failed");
}
}
