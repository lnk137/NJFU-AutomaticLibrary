#pragma once
#include <sqlite3.h>
#include "platform.hpp"
#include <sys/file.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <unistd.h>
#include <atomic>
#include <cstdint>
#include <fstream>
#include <iomanip>
#include <map>
#include <memory>
#include <mutex>
#include <shared_mutex>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>
#include "profile.hpp"

inline thread_local bool inside_final_guard=false;
inline thread_local uint64_t final_guard_sql=0;
inline uint64_t now_ns() { return native_platform::continuous_ns(); }
inline std::string hash(const std::string &value) { return native_platform::hash(value); }
inline bool token(const std::string &s) {return !s.empty() && s.size()<=128 && s.find_first_not_of("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")==std::string::npos;}
inline uint64_t number(const std::string &s) {size_t n=0;auto v=std::stoull(s,&n);if(n!=s.size() || s.empty() || s[0]=='-')throw std::runtime_error("invalid integer");return v;}
using Row=std::map<std::string,std::string>;
struct StorageFailure:std::runtime_error{using std::runtime_error::runtime_error;};
inline const std::string claim_sql="UPDATE jobs SET claimed=1,status='reserved' WHERE id=? AND epoch=? AND session=? AND claimed=0 AND status='staged'";

class Database {
    sqlite3 *handle=nullptr;
    std::map<std::string,sqlite3_stmt *> cached;
public:
    Database(const std::string &path,bool create) {
        int flags=SQLITE_OPEN_READWRITE|SQLITE_OPEN_FULLMUTEX|(create?SQLITE_OPEN_CREATE:0);
        if(sqlite3_open_v2(path.c_str(),&handle,flags,"njfu-phase-timing")!=SQLITE_OK)throw std::runtime_error("ledger open failed");
        sqlite3_busy_timeout(handle,2000);
        // Reject copied state before PRAGMA or epoch changes. Legacy is Mac-only.
        if(!create){
            auto system=query("SELECT value FROM meta WHERE key='platform'");
            auto profile=query("SELECT value FROM meta WHERE key='durability_profile'");
            bool legacy=system.empty() && profile.empty() && std::string(native_platform::name)=="darwin";
            if(!legacy && (system.size()!=1 || profile.size()!=1 || system[0]["value"]!=native_platform::name || profile[0]["value"]!=native_platform::durability_profile))
                throw std::runtime_error("ledger platform/durability profile mismatch");
        }
        run("PRAGMA journal_mode="+options.journal);run("PRAGMA synchronous=EXTRA");run("PRAGMA fullfsync="+std::to_string(native_platform::fullfsync));
        run("PRAGMA checkpoint_fullfsync="+std::to_string(native_platform::fullfsync));
        std::string expected=options.journal;for(char &c:expected)c=static_cast<char>(tolower(c));
        if(query("PRAGMA journal_mode")[0].begin()->second!=expected)throw std::runtime_error("journal mode unavailable");
        if(query("PRAGMA synchronous")[0].begin()->second!="3" || query("PRAGMA fullfsync")[0].begin()->second!=std::to_string(native_platform::fullfsync) || query("PRAGMA checkpoint_fullfsync")[0].begin()->second!=std::to_string(native_platform::fullfsync))throw std::runtime_error("ledger durability unavailable");
        if(query("PRAGMA integrity_check")[0].begin()->second!="ok")throw std::runtime_error("ledger corrupt");
    }
    ~Database(){for(auto &item:cached)sqlite3_finalize(item.second);if(handle)sqlite3_close(handle);}
    void prime(const std::string &sql){
        if(!options.prepared || cached.count(sql))return;
        sqlite3_stmt *statement=nullptr;
        if(sqlite3_prepare_v3(handle,sql.c_str(),-1,SQLITE_PREPARE_PERSISTENT,&statement,nullptr)!=SQLITE_OK)throw StorageFailure("ledger precompile failed");
        cached.emplace(sql,statement);
    }
    std::vector<Row> query(const std::string &sql,const std::vector<std::string> &args={}) {
        if(inside_final_guard)++final_guard_sql;
        sqlite3_stmt *statement=nullptr;
        if(options.prepared){auto found=cached.find(sql);if(found!=cached.end())statement=found->second;}
        if(!statement){
            uint64_t prepare_start=now_ns();int prepared=sqlite3_prepare_v3(handle,sql.c_str(),-1,options.prepared?SQLITE_PREPARE_PERSISTENT:0,&statement,nullptr);
            if(active_sql_profile){active_sql_profile->prepare_ns+=now_ns()-prepare_start;++active_sql_profile->prepare_calls;}
            if(prepared!=SQLITE_OK)throw StorageFailure("ledger prepare failed");
            if(options.prepared)cached.emplace(sql,statement);
        }
        struct Final {sqlite3_stmt *s;bool reuse;~Final(){if(reuse){sqlite3_reset(s);sqlite3_clear_bindings(s);}else sqlite3_finalize(s);}} final{statement,options.prepared};
        uint64_t bind_start=now_ns();
        for(size_t i=0;i<args.size();++i)if(sqlite3_bind_text(statement,static_cast<int>(i+1),args[i].c_str(),-1,SQLITE_TRANSIENT)!=SQLITE_OK)throw StorageFailure("ledger bind failed");
        if(active_sql_profile)active_sql_profile->bind_ns+=now_ns()-bind_start;
        uint64_t step_start=now_ns();
        std::vector<Row> result;int status;
        while((status=sqlite3_step(statement))==SQLITE_ROW){
            Row row;
            for(int i=0;i<sqlite3_column_count(statement);++i){const auto *p=sqlite3_column_text(statement,i);row[sqlite3_column_name(statement,i)]=p?reinterpret_cast<const char *>(p):"";}
            result.push_back(std::move(row));
        }
        if(active_sql_profile)active_sql_profile->step_ns+=now_ns()-step_start;
        if(status!=SQLITE_DONE)throw StorageFailure("ledger operation failed");
        return result;
    }
    void run(const std::string &sql,const std::vector<std::string> &args={}){query(sql,args);}
    int changes(){return sqlite3_changes(handle);}
    void rollback(){try{run("ROLLBACK");}catch(...) {}}
};

struct State {
    uint64_t version=0,credential=0;
    bool enabled=false,plan=false;
    std::string fingerprint;
    std::string binding(const std::string &key) const {return key+"|"+std::to_string(version)+"|"+(enabled?"1":"0")+"|"+(plan?"1":"0")+"|"+std::to_string(credential);}
};
struct Slot {std::shared_mutex gate;std::atomic<bool> pending{false};State state;};
struct Job;
struct Batch {std::mutex mutex;bool ready=false;uint64_t claim_finished=0;SqlProfile profile;std::vector<std::weak_ptr<Job>> jobs;};
struct Job {
    std::string id,account,day,kind,operation,body,fingerprint,binding;
    unsigned worker=0;uint64_t account_version=0,global_version=0,credential=0,due=0,expires=0,created=0,epoch=0,session=0,pilot_wall_due=0;
    bool preclaim=false;std::atomic<bool> claim_ok{false},wire_used{false},started{false};
    std::shared_ptr<Slot> slot;std::shared_ptr<Batch> batch;
};

class Authority {
    int lock_fd=-1;
    std::unique_ptr<Database> db;
    std::mutex db_mutex,jobs_mutex;
    std::map<std::string,std::shared_ptr<Slot>> accounts;
    std::map<std::string,std::shared_ptr<Job>> jobs;
public:
    std::shared_ptr<Slot> global=std::make_shared<Slot>();
    std::atomic<bool> poisoned{false},owner_alive{false},connected{false},stopping{false};
    std::atomic<uint64_t> session{0},lease_until{0};
    std::atomic<int> fault{0};
    uint64_t epoch=0;std::string identity;

    Authority(const std::string &directory,bool initialize){
        struct stat info{};
        if(lstat(directory.c_str(),&info) || !S_ISDIR(info.st_mode) || info.st_uid!=getuid() || (info.st_mode&077))throw std::runtime_error("private state directory required");
        std::string lock=directory+"/owner.lock";
        lock_fd=open(lock.c_str(),O_RDWR|O_CREAT|O_NOFOLLOW,0600);
        if(lock_fd<0 || flock(lock_fd,LOCK_EX|LOCK_NB))throw std::runtime_error("executor already owns ledger");
        std::string marker=directory+"/identity",path=directory+"/state.db";
        bool marker_exists=lstat(marker.c_str(),&info)==0;
        bool ledger_exists=lstat(path.c_str(),&info)==0;
        if(initialize){
            if(marker_exists || ledger_exists)throw std::runtime_error("refuse ledger reset");
            unsigned char random[16];native_platform::random_bytes(random,sizeof(random));
            identity=hash(std::string(reinterpret_cast<char *>(random),sizeof(random)));
            int fd=open(marker.c_str(),O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW,0600);
            if(fd<0 || write(fd,identity.data(),identity.size())!=static_cast<ssize_t>(identity.size()) || native_platform::sync_file(fd))throw std::runtime_error("identity persistence failed");
            close(fd);
            int parent=open(directory.c_str(),O_RDONLY|O_DIRECTORY);if(parent<0 || fsync(parent))throw std::runtime_error("state directory sync failed");close(parent);
        } else {
            if(!marker_exists || !ledger_exists)throw std::runtime_error("missing persistent ledger or identity");
            for(const auto &file:{marker,path}){if(lstat(file.c_str(),&info) || !S_ISREG(info.st_mode) || info.st_uid!=getuid() || (info.st_mode&077) || info.st_nlink!=1)throw std::runtime_error("unsafe state file");}
            // Preserve fail-closed corruption policy even if a valid WAL could repair a damaged main header.
            std::ifstream ledger_header(path,std::ios::binary);char header[16]{};ledger_header.read(header,16);
            if(ledger_header.gcount()!=16 || std::string(header,16)!=std::string("SQLite format 3\0",16))throw std::runtime_error("invalid ledger header");
            std::ifstream input(marker);input>>identity;if(identity.size()!=64)throw std::runtime_error("invalid state identity");
        }
        db=std::make_unique<Database>(path,initialize);
        if(initialize){
            db->run("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)");
            db->run("CREATE TABLE state(key TEXT PRIMARY KEY,version INTEGER NOT NULL,enabled INTEGER NOT NULL,plan INTEGER NOT NULL,credential INTEGER NOT NULL,fingerprint TEXT NOT NULL)");
            db->run("CREATE TABLE jobs(id TEXT PRIMARY KEY,account TEXT NOT NULL,day TEXT NOT NULL,kind TEXT NOT NULL,operation TEXT NOT NULL,epoch INTEGER NOT NULL,session INTEGER NOT NULL,binding TEXT NOT NULL,claimed INTEGER NOT NULL,status TEXT NOT NULL,UNIQUE(account,day,kind,operation))");
            db->run("INSERT INTO meta VALUES('platform',?)",{native_platform::name});
            db->run("INSERT INTO meta VALUES('durability_profile',?)",{native_platform::durability_profile});
            db->run("INSERT INTO meta VALUES('identity',?)",{identity});db->run("INSERT INTO meta VALUES('epoch','0')");db->run("INSERT INTO meta VALUES('format_version','1')");
        }
        auto format=db->query("SELECT value FROM meta WHERE key='format_version'");if(format.size()!=1 || format[0]["value"]!="1")throw std::runtime_error("unsupported ledger format");
        auto meta=db->query("SELECT value FROM meta WHERE key='identity'");if(meta.size()!=1 || meta[0]["value"]!=identity)throw std::runtime_error("ledger identity mismatch");
        auto epochs=db->query("SELECT value FROM meta WHERE key='epoch'");if(epochs.size()!=1)throw std::runtime_error("missing epoch");
        epoch=number(epochs[0]["value"])+1;
        db->run("BEGIN IMMEDIATE");
        try {
            db->run("UPDATE meta SET value=? WHERE key='epoch'",{std::to_string(epoch)});
            db->run("UPDATE jobs SET status=CASE WHEN claimed=1 THEN 'uncertain' ELSE 'orphaned' END WHERE status NOT IN ('done','uncertain','denied')");
            db->run("COMMIT");
        }catch(StorageFailure &){db->rollback();poisoned=true;throw;}catch(...){db->rollback();throw;}
        for(auto &row:db->query("SELECT * FROM state")){
            auto slot=row["key"]=="global"?global:std::make_shared<Slot>();
            slot->state={number(row["version"]),number(row["credential"]),row["enabled"]=="1",row["plan"]=="1",row["fingerprint"]};
            if(slot->state.fingerprint!=hash(slot->state.binding(row["key"])))throw std::runtime_error("state fingerprint mismatch");
            if(row["key"]!="global")accounts[row["key"]]=slot;
        }
    }
    ~Authority(){if(lock_fd>=0){flock(lock_fd,LOCK_UN);close(lock_fd);}}
    uint64_t connect_owner(){
        bool expected=false;if(!connected.compare_exchange_strong(expected,true))throw std::runtime_error("control owner busy");
        global->pending=true;std::unique_lock guard(global->gate);
        lease_until=0;owner_alive=true;uint64_t value=++session;global->pending=false;return value;
    }
    void disconnect_owner(uint64_t value){if(session==value){owner_alive=false;lease_until=0;connected=false;}}
    void header(uint64_t boot,uint64_t owner){if(boot!=epoch || owner!=session || !owner_alive)throw std::runtime_error("stale epoch or control session");}
    void lease(uint64_t boot,uint64_t owner,uint64_t milliseconds){header(boot,owner);if(milliseconds<10 || milliseconds>2000)throw std::runtime_error("invalid lease");lease_until=now_ns()+milliseconds*1000000;}
    void state(uint64_t boot,uint64_t owner,const std::string &key,const State &desired){
        header(boot,owner);if(!token(key) || !desired.version || desired.fingerprint!=hash(desired.binding(key)))throw std::runtime_error("invalid state");
        auto slot=global;
        if(key!="global"){auto it=accounts.find(key);if(it==accounts.end())it=accounts.emplace(key,std::make_shared<Slot>()).first;slot=it->second;}
        slot->pending=true;
        struct Clear {std::shared_ptr<Slot> s;~Clear(){s->pending=false;}} clear{slot};
        std::shared_lock<std::shared_mutex> global_read;
        if(key!="global")global_read=std::shared_lock<std::shared_mutex>(global->gate);
        std::unique_lock write(slot->gate);
        auto old=slot->state;
        if(desired.version==old.version){if(desired.fingerprint!=old.fingerprint)throw std::runtime_error("conflicting version");return;}
        if(desired.version<old.version)throw std::runtime_error("stale version");
        if(desired.version!=old.version+1)throw std::runtime_error("version gap");
        std::lock_guard storage(db_mutex);
        try{
            db->run("BEGIN IMMEDIATE");
            db->run("INSERT OR REPLACE INTO state VALUES(?,?,?,?,?,?)",{key,std::to_string(desired.version),desired.enabled?"1":"0",desired.plan?"1":"0",std::to_string(desired.credential),desired.fingerprint});
            db->run("COMMIT");if(fault==3)_exit(83);
            slot->state=desired;
        }catch(...){db->rollback();poisoned=true;throw;}
    }
    bool valid(const std::shared_ptr<Job> &job,bool claimed=true){
        uint64_t now=now_ns();const auto &account=job->slot->state;const auto &master=global->state;
        return !poisoned && !stopping && owner_alive && lease_until>=now && epoch==job->epoch && session==job->session
            && !global->pending && !job->slot->pending && master.enabled && master.plan && account.enabled && account.plan
            && account.version==job->account_version && master.version==job->global_version && account.credential==job->credential
            && now>=job->due && now<=job->expires && now>=job->created && now-job->created<=2000000000ULL
            && (!claimed || job->claim_ok);
    }
    void arm(uint64_t boot,uint64_t owner,const std::vector<std::shared_ptr<Job>> &items,bool preclaim){
        header(boot,owner);if(items.empty() || items.size()>24 || poisoned)throw std::runtime_error("invalid or poisoned batch");
        std::shared_lock master(global->gate);std::lock_guard storage(db_mutex);std::lock_guard memory(jobs_mutex);
        auto batch=std::make_shared<Batch>();unsigned existing=0;
        for(auto &job:items){
            auto it=accounts.find(job->account);uint64_t now=now_ns();
            if(it==accounts.end() || !token(job->id) || job->day.size()!=10 || (job->kind!="S" && job->kind!="M") ||
               (job->kind=="S" && job->operation!="-") || !token(job->operation) || job->body.empty() || job->body.size()>16384 ||
               job->due<now+20000000 || job->due<job->created || job->due-job->created>2000000000ULL ||
               job->created>now || job->expires<job->due || job->expires-job->due>30000000000ULL || job->due!=items[0]->due ||
               job->fingerprint!=hash(job->body))throw std::runtime_error("invalid job binding or preparation window");
            job->slot=it->second;job->epoch=boot;job->session=owner;job->preclaim=preclaim;job->batch=batch;
            if(job->account_version!=job->slot->state.version || job->global_version!=global->state.version || job->credential!=job->slot->state.credential || !job->slot->state.enabled || !job->slot->state.plan || !global->state.enabled || !global->state.plan)throw std::runtime_error("job state mismatch");
            auto binding=job->account+"|"+job->day+"|"+job->kind+"|"+job->operation+"|"+std::to_string(job->account_version)+"|"+std::to_string(job->global_version)+"|"+std::to_string(job->credential)+"|"+std::to_string(job->due)+"|"+std::to_string(job->expires)+"|"+std::to_string(job->created)+"|"+job->fingerprint+"|"+(preclaim?"P":"D");
            if(job->pilot_wall_due)binding+="|PILOT1|"+std::to_string(job->pilot_wall_due);
            job->binding=hash(binding);
            auto found=jobs.find(job->id);
            if(found!=jobs.end() && found->second->epoch==boot && found->second->session==owner){if(found->second->binding!=job->binding)throw std::runtime_error("job fingerprint conflict");++existing;}
            batch->jobs.push_back(job);
        }
        if(existing){if(existing!=items.size())throw std::runtime_error("mixed idempotent batch");return;}
        try{
            db->run("BEGIN IMMEDIATE");
            for(auto &job:items){
                auto rows=db->query("SELECT * FROM jobs WHERE id=? OR (account=? AND day=? AND kind=? AND operation=?)",{job->id,job->account,job->day,job->kind,job->operation});
                if(rows.empty())db->run("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?)",{job->id,job->account,job->day,job->kind,job->operation,std::to_string(boot),std::to_string(owner),job->binding,preclaim?"1":"0",preclaim?"reserved":"staged"});
                else {
                    auto &row=rows[0];
                    if(row["account"]!=job->account || row["day"]!=job->day || row["kind"]!=job->kind || row["operation"]!=job->operation)throw std::runtime_error("immutable task domain mismatch");
                    if(rows.size()!=1 || row["id"]!=job->id || row["claimed"]!="0" || (number(row["epoch"])==boot && number(row["session"])==owner))throw std::runtime_error("persistent task already spent");
                    db->run("UPDATE jobs SET epoch=?,session=?,binding=?,claimed=?,status=? WHERE id=? AND claimed=0",{std::to_string(boot),std::to_string(owner),job->binding,preclaim?"1":"0",preclaim?"reserved":"staged",job->id});
                }
            }
            if(fault==1)_exit(81);
            db->run("COMMIT");if(fault==2)_exit(82);
            if(!preclaim){db->prime("BEGIN IMMEDIATE");db->prime(claim_sql);db->prime("COMMIT");}
            batch->ready=preclaim;batch->claim_finished=preclaim?now_ns():0;
            for(auto &job:items){job->claim_ok=preclaim;jobs[job->id]=job;}
        }catch(StorageFailure &){db->rollback();poisoned=true;throw;}catch(...){db->rollback();throw;}
    }
    std::shared_ptr<Job> find_job(const std::string &id){std::lock_guard lock(jobs_mutex);auto it=jobs.find(id);if(it==jobs.end())throw std::runtime_error("unknown job");return it->second;}
    uint64_t claim_batch(const std::shared_ptr<Batch> &batch){
        uint64_t start=now_ns();std::lock_guard batch_lock(batch->mutex);uint64_t wait=now_ns()-start;if(batch->ready)return wait;
        uint64_t db_start=now_ns();std::lock_guard storage(db_mutex);batch->profile.db_wait_ns=now_ns()-db_start;
        ProfileScope profiling(batch->profile);std::vector<std::shared_ptr<Job>> claimed;
        try{
            uint64_t begin=now_ns();db->run("BEGIN IMMEDIATE");batch->profile.begin_ns=now_ns()-begin;
            uint64_t updates=now_ns();
            for(auto &weak:batch->jobs)if(auto job=weak.lock()){
                db->run(claim_sql,{job->id,std::to_string(job->epoch),std::to_string(job->session)});
                if(db->changes()==1)claimed.push_back(job);
            }
            batch->profile.updates_ns=now_ns()-updates;
            uint64_t commit=now_ns();db->run("COMMIT");batch->profile.commit_ns=now_ns()-commit;if(fault==4)_exit(84);
            for(auto &job:claimed)job->claim_ok=true;
        }catch(...){db->rollback();poisoned=true;}
        batch->claim_finished=now_ns();batch->ready=true;return wait;
    }
    bool outcome(const std::shared_ptr<Job> &job,bool complete){
        std::lock_guard storage(db_mutex);
        try{db->run("UPDATE jobs SET status=? WHERE id=? AND epoch=? AND session=?",{job->wire_used?(complete?"done":"uncertain"):"denied",job->id,std::to_string(job->epoch),std::to_string(job->session)});return true;}
        catch(...){poisoned=true;return false;}
    }
    void readonly(){std::lock_guard lock(db_mutex);db->run("PRAGMA query_only=ON");}
    Row job_status(const std::string &id){std::lock_guard lock(db_mutex);auto rows=db->query("SELECT id,epoch,session,claimed,status FROM jobs WHERE id=?",{id});return rows.empty()?Row{}:rows[0];}
    std::string config(){
        std::lock_guard lock(db_mutex);
        auto value=[&](const std::string &key){return db->query("PRAGMA "+key)[0].begin()->second;};
        return "{\"platform\":\""+std::string(native_platform::name)+"\",\"durability_profile\":\""+native_platform::durability_profile+"\",\"variant\":\""+options.variant+"\",\"journal\":\""+value("journal_mode")+"\",\"synchronous\":"+value("synchronous")+",\"fullfsync\":"+value("fullfsync")+",\"checkpoint_fullfsync\":"+value("checkpoint_fullfsync")+",\"wal_autocheckpoint\":"+value("wal_autocheckpoint")+",\"prepared\":"+(options.prepared?"true":"false")+",\"loops\":"+std::to_string(options.loops)+",\"profile_version\":1}";
    }
};
