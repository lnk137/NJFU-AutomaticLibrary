// Fixed-purpose school sender; durable due-time claims and private control.
#include "authority.hpp"
#include <curl/curl.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <sys/resource.h>
#include <poll.h>
#include <cstring>
#include <iostream>
#include <thread>
#include <set>
#include "peer_threads.hpp"
#include "wire.hpp"
#include <chrono>
#include <cstdlib>
#include <ctime>
#include <algorithm>

static bool fixture_endpoint=false;
static int target_port=443;
static std::string target_base="https://libseat.njfu.edu.cn",target_ca;

static bool send_all(int fd,const std::string &s){size_t done=0;while(done<s.size()){ssize_t n=send(fd,s.data()+done,s.size()-done,native_platform::send_flags);if(n<=0)return false;done+=static_cast<size_t>(n);}return true;}
static bool read_line(int fd,std::string &s){s.clear();char c;while(s.size()<32768){if(recv(fd,&c,1,0)!=1)return false;if(c=='\n')return true;s+=c;}return false;}
static bool read_body(int fd,size_t n,std::string &s){if(n>16384)return false;s.assign(n,'\0');size_t done=0;while(done<n){ssize_t k=recv(fd,s.data()+done,n-done,0);if(k<=0)return false;done+=static_cast<size_t>(k);}return true;}
static void wait_until(uint64_t due,Authority &authority){
    for(;;){uint64_t now=now_ns();if(now>=due || authority.stopping)return;uint64_t remaining=due-now;
        if(remaining>100000){uint64_t sleep=remaining-100000;if(sleep>20000000)sleep-=20000000;else if(sleep>1000000)sleep=1000000;
            native_platform::sleep_for_ns(sleep);}
    }
}
static bool wall_now_ns(uint64_t &value){
    timespec wall{};
    if(clock_gettime(CLOCK_REALTIME,&wall) || wall.tv_sec<0 || wall.tv_nsec<0 || wall.tv_nsec>=1000000000L)return false;
    auto ns=static_cast<__uint128_t>(wall.tv_sec)*1000000000ULL+static_cast<uint64_t>(wall.tv_nsec);
    if(ns==0 || ns>UINT64_MAX)return false;
    value=static_cast<uint64_t>(ns);return true;
}
static bool wait_for_job(const std::shared_ptr<Job> &job,Authority &authority,uint64_t &fault){
    if(!job->pilot_wall_due){wait_until(job->due,authority);return true;}
    for(;;){
        uint64_t now=now_ns(),wall=0;
        if(authority.stopping || now>=job->expires)return false;
        if(!wall_now_ns(wall)){fault=1;return false;}
        if(now>=job->due && wall>=job->pilot_wall_due)return true;
        uint64_t continuous=now<job->due?job->due-now:0;
        uint64_t civil=wall<job->pilot_wall_due?job->pilot_wall_due-wall:0;
        uint64_t remaining=std::min(std::max(continuous,civil),job->expires-now);
        if(remaining>100000){uint64_t sleep=remaining-100000;if(sleep>20000000)sleep-=20000000;else if(sleep>1000000)sleep=1000000;
            native_platform::sleep_for_ns(sleep);}
    }
}
static size_t receive(char *p,size_t a,size_t b,void *data){auto &out=*static_cast<std::string *>(data);if(out.size()+a*b>32768)return 0;out.append(p,a*b);return a*b;}
struct Request {Authority *authority;std::shared_ptr<Job> job;int fd;uint64_t checked=0,sent=0,account_version=0,global_version=0;
    uint64_t gate_wait_ns=0,batch_wait_ns=0,guard_ns=0,curl_start_ns=0,guard_sql=0,network_loop=0,pilot_wall_error=0;bool authorized=false;};
static int prereq(void *data,char *primary,char *,int port,int){
    auto &request=*static_cast<Request *>(data);auto &authority=*request.authority;
    uint64_t start=now_ns(),sql_before=final_guard_sql;inside_final_guard=true;struct Leave {Request &r;uint64_t start,sql_before;~Leave(){inside_final_guard=false;r.guard_ns+=now_ns()-start;r.guard_sql+=final_guard_sql-sql_before;}} leave{request,start,sql_before};
    request.checked=now_ns();
    request.account_version=request.job->slot->state.version;request.global_version=authority.global->state.version;
    if((fixture_endpoint && std::string(primary)!="127.0.0.1") || port!=target_port || !authority.valid(request.job))return CURL_PREREQFUNC_ABORT;
    if(request.job->pilot_wall_due){uint64_t wall=0;
        if(!wall_now_ns(wall)){request.pilot_wall_error=1;return CURL_PREREQFUNC_ABORT;}
        if(wall<request.job->pilot_wall_due || !authority.valid(request.job))return CURL_PREREQFUNC_ABORT;}
    bool unused=false;if(!request.job->wire_used.compare_exchange_strong(unused,true))return CURL_PREREQFUNC_ABORT;
    request.authorized=true;
    return CURL_PREREQFUNC_OK;
}
static int debug(CURL *,curl_infotype type,char *,size_t,void *data){if(type==CURLINFO_HEADER_OUT)static_cast<Request *>(data)->sent=now_ns();return 0;}
static std::string metrics(const Request &r){
    const auto &p=r.job->batch->profile;std::ostringstream out;out<<"METRICS {";
    bool first=true;auto field=[&](const char *name,uint64_t value){if(!first)out<<',';first=false;out<<'\"'<<name<<"\":"<<value;};
    field("gate_wait_ns",r.gate_wait_ns);field("batch_wait_ns",r.batch_wait_ns);field("guard_ns",r.guard_ns);field("curl_start_ns",r.curl_start_ns);field("network_loop",r.network_loop);
    if(r.job->pilot_wall_due)field("pilot_wall_error",r.pilot_wall_error);
    field("db_wait_ns",p.db_wait_ns);field("begin_ns",p.begin_ns);field("updates_ns",p.updates_ns);field("commit_ns",p.commit_ns);
    field("prepare_ns",p.prepare_ns);field("prepare_calls",p.prepare_calls);field("bind_ns",p.bind_ns);field("step_ns",p.step_ns);
    field("sync_ns",p.sync_ns);field("sync_calls",p.sync_calls);field("sync_flags",p.sync_flags);field("directory_sync_ns",p.directory_sync_ns);field("write_ns",p.write_ns);field("write_calls",p.write_calls);
    out<<"}\n";return out.str();
}


static std::shared_ptr<Job> parse_job(int fd,const std::string &line){
    std::istringstream input(line);auto job=std::make_shared<Job>();std::string worker,av,gv,cred,due,expires,created,size;
    if(!(input>>job->id>>worker>>job->account>>job->day>>job->kind>>job->operation>>av>>gv>>cred>>due>>expires>>created>>job->fingerprint>>size))throw std::runtime_error("invalid job frame");
    job->worker=static_cast<unsigned>(number(worker));job->account_version=number(av);job->global_version=number(gv);job->credential=number(cred);
    job->due=number(due);job->expires=number(expires);job->created=number(created);
    std::string marker,wall,extra;
    if(input>>marker){
        if(marker!="PILOT1" || !(input>>wall) || (input>>extra))throw std::runtime_error("invalid pilot frame");
        job->pilot_wall_due=number(wall);
        if(!job->pilot_wall_due)throw std::runtime_error("invalid pilot wall deadline");
    }
    if(!read_body(fd,number(size),job->body))throw std::runtime_error("incomplete job body");
    school_request(job->body);
    return job;
}
static void control(int fd,Authority &authority,unsigned workers){
    uint64_t session=0;
    try{
        session=authority.connect_owner();
        if(!send_all(fd,"HELLO 1 "+authority.identity+" "+std::to_string(authority.epoch)+" "+std::to_string(session)+"\n"))throw std::runtime_error("control lost");
        std::string line;
        while(read_line(fd,line)){
            try{
                std::istringstream input(line);std::string command,boot_s,owner_s;
                if(!(input>>command>>boot_s>>owner_s))throw std::runtime_error("missing control header");
                auto boot=number(boot_s),owner=number(owner_s);authority.header(boot,owner);
                if(command=="STATE"){
                    std::string key,v,e,p,c,fingerprint;if(!(input>>key>>v>>e>>p>>c>>fingerprint) || (e!="0" && e!="1") || (p!="0" && p!="1"))throw std::runtime_error("bad state frame");
                    State desired{number(v),number(c),e=="1",p=="1",fingerprint};authority.state(boot,owner,key,desired);
                    if(!send_all(fd,"ACK STATE "+key+" "+v+" "+fingerprint+"\n"))break;
                }else if(command=="LEASE"){
                    std::string ttl;if(!(input>>ttl))throw std::runtime_error("bad lease frame");authority.lease(boot,owner,number(ttl));
                    if(!send_all(fd,"ACK LEASE\n"))break;
                }else if(command=="ARM"){
                    std::string count_s,mode;if(!(input>>count_s>>mode) || mode!="D")throw std::runtime_error("bad arm header");
                    uint64_t count=number(count_s);if(count<1 || count>workers)throw std::runtime_error("invalid batch size");
                    std::vector<std::shared_ptr<Job>> jobs;
                    for(uint64_t i=0;i<count;++i){if(!read_line(fd,line))throw std::runtime_error("incomplete arm frame");auto job=parse_job(fd,line);if(job->worker>=workers)throw std::runtime_error("worker out of range");jobs.push_back(job);}
                    authority.arm(boot,owner,jobs,mode=="P");
                    if(!send_all(fd,"ACK ARM "+count_s+" "+std::to_string(now_ns())+"\n"))break;
                }else if(command=="STATUS"){
                    if(!send_all(fd,"STATUS "+std::to_string(now_ns())+" "+std::to_string(authority.poisoned)+" "+std::to_string(authority.lease_until.load())+"\n"))break;
                }else if(command=="CONFIG"){
                    std::string config=authority.config();config.pop_back();
                    config+=std::string(",\"wire_protocol\":\"campus-v1\",\"pilot_wall_v1\":true,\"endpoint_mode\":\"")+(fixture_endpoint?"fixture":"school")+"\"}";
                    if(!send_all(fd,"CONFIG "+config+"\n"))break;
                }else if(command=="JOB"){
                    std::string id;if(!(input>>id) || !token(id))throw std::runtime_error("invalid job id");auto row=authority.job_status(id);
                    std::string reply=row.empty()?"NOTFOUND":"JOB "+row["id"]+" "+row["epoch"]+" "+row["session"]+" "+row["claimed"]+" "+row["status"];
                    if(!send_all(fd,reply+"\n"))break;
                }else if(command=="QUIT"){
                    authority.stopping=true;send_all(fd,"ACK QUIT\n");break;
                }else throw std::runtime_error("unknown control command");
            }catch(const StorageFailure &e){authority.poisoned=true;if(!send_all(fd,std::string("ERROR ")+e.what()+"\n"))break;}
            catch(const std::exception &e){if(!send_all(fd,std::string("ERROR ")+e.what()+"\n"))break;}
        }
    }catch(const std::exception &e){send_all(fd,std::string("ERROR ")+e.what()+"\n");}
    if(session)authority.disconnect_owner(session);
}


static void worker(int fd,unsigned index,Authority &authority){
    native_platform::worker_priority();
    CURL *handle=curl_easy_init();curl_slist *headers=nullptr;std::string response;
    try{
        if(!handle)throw std::runtime_error("curl init failed");
        curl_easy_setopt(handle,CURLOPT_PROXY,"");curl_easy_setopt(handle,CURLOPT_PROTOCOLS_STR,"https");
        curl_easy_setopt(handle,CURLOPT_FOLLOWLOCATION,0L);curl_easy_setopt(handle,CURLOPT_SSL_VERIFYPEER,1L);curl_easy_setopt(handle,CURLOPT_SSL_VERIFYHOST,2L);
        if(!target_ca.empty())curl_easy_setopt(handle,CURLOPT_CAINFO,target_ca.c_str());
        curl_easy_setopt(handle,CURLOPT_NOSIGNAL,1L);curl_easy_setopt(handle,CURLOPT_HTTP_VERSION,CURL_HTTP_VERSION_1_1);curl_easy_setopt(handle,CURLOPT_TCP_NODELAY,1L);
        curl_easy_setopt(handle,CURLOPT_USERAGENT,"NJFU-Seat-Executor/1.0");curl_easy_setopt(handle,CURLOPT_WRITEFUNCTION,receive);curl_easy_setopt(handle,CURLOPT_WRITEDATA,&response);
        if(!send_all(fd,"READY "+std::to_string(now_ns())+"\n"))throw std::runtime_error("worker lost");
        std::string line;
        while(read_line(fd,line)){
            try{
                if(line=="QUIT")break;
                if(line=="CLOCK"){send_all(fd,std::to_string(now_ns())+"\n");continue;}
                std::istringstream input(line);std::string command,id;if(!(input>>command>>id))throw std::runtime_error("bad worker command");
                if(command=="WARM"){
                    uint64_t budget=number(id);if(budget<50 || budget>3500)throw std::runtime_error("invalid warm budget");
                    std::string url=target_base+"/ic-web/sysConfig/public";response.clear();
                    curl_easy_setopt(handle,CURLOPT_HTTPHEADER,nullptr);curl_easy_setopt(handle,CURLOPT_POSTFIELDS,nullptr);
                    curl_easy_setopt(handle,CURLOPT_POST,0L);curl_easy_setopt(handle,CURLOPT_HTTPGET,1L);
                    curl_easy_setopt(handle,CURLOPT_PREREQFUNCTION,nullptr);curl_easy_setopt(handle,CURLOPT_PREREQDATA,nullptr);
                    curl_easy_setopt(handle,CURLOPT_VERBOSE,0L);curl_easy_setopt(handle,CURLOPT_DEBUGFUNCTION,nullptr);curl_easy_setopt(handle,CURLOPT_DEBUGDATA,nullptr);
                    curl_easy_setopt(handle,CURLOPT_URL,url.c_str());curl_easy_setopt(handle,CURLOPT_TIMEOUT_MS,static_cast<long>(budget));
                    curl_easy_setopt(handle,CURLOPT_CONNECTTIMEOUT_MS,static_cast<long>(std::min<uint64_t>(budget,1500)));
                    CURLcode result=curl_easy_perform(handle);long http=0,connections=0;
                    curl_easy_getinfo(handle,CURLINFO_RESPONSE_CODE,&http);curl_easy_getinfo(handle,CURLINFO_NUM_CONNECTS,&connections);
                    response.clear();if(result!=CURLE_OK || http!=200)throw std::runtime_error("verified TLS warmup failed");
                    if(!send_all(fd,"WARMED "+std::to_string(now_ns())+" "+std::to_string(connections)+"\n"))break;
                    continue;
                }
                if(command!="WAIT")throw std::runtime_error("bad worker command");
                auto job=authority.find_job(id);if(job->worker!=index)throw std::runtime_error("wrong worker binding");
                bool unused=false;if(!job->started.compare_exchange_strong(unused,true))throw std::runtime_error("duplicate wait");
                Request request{&authority,job,fd};final_guard_sql=0;
                auto packet=school_request(job->body);std::string payload=std::move(packet.body);
                curl_easy_setopt(handle,CURLOPT_HTTPHEADER,nullptr);curl_slist_free_all(headers);headers=nullptr;
                for(const auto &header:packet.headers)headers=curl_slist_append(headers,(header.first+": "+header.second).c_str());
                headers=curl_slist_append(headers,"Host: libseat.njfu.edu.cn");headers=curl_slist_append(headers,"Expect:");
                curl_easy_setopt(handle,CURLOPT_HTTPHEADER,headers);
                std::string url=target_base+"/ic-web/reserve/seat";response.clear();
                curl_easy_setopt(handle,CURLOPT_URL,url.c_str());curl_easy_setopt(handle,CURLOPT_POST,1L);curl_easy_setopt(handle,CURLOPT_POSTFIELDS,payload.data());curl_easy_setopt(handle,CURLOPT_POSTFIELDSIZE,static_cast<long>(payload.size()));
                curl_easy_setopt(handle,CURLOPT_CONNECTTIMEOUT_MS,2000L);curl_easy_setopt(handle,CURLOPT_TIMEOUT_MS,3000L);
                curl_easy_setopt(handle,CURLOPT_PREREQFUNCTION,prereq);curl_easy_setopt(handle,CURLOPT_PREREQDATA,&request);
                curl_easy_setopt(handle,CURLOPT_VERBOSE,1L);curl_easy_setopt(handle,CURLOPT_DEBUGFUNCTION,debug);curl_easy_setopt(handle,CURLOPT_DEBUGDATA,&request);
                if(!send_all(fd,"WAITING\n"))throw std::runtime_error("worker lost");
                uint64_t wake=0,network_done=0;CURLcode status=CURLE_ABORTED_BY_CALLBACK;long http=0,connections=0;
                bool release_ready=wait_for_job(job,authority,request.pilot_wall_error);wake=now_ns();
                {
                    uint64_t gate_start=now_ns();
                    std::shared_lock master(authority.global->gate);std::shared_lock account(job->slot->gate);
                    request.gate_wait_ns=now_ns()-gate_start;
                    if(release_ready && authority.valid(job,false)){
                        request.batch_wait_ns=authority.claim_batch(job->batch);
                        if(authority.valid(job)){request.curl_start_ns=now_ns();status=curl_easy_perform(handle);}
                    }
                    curl_easy_getinfo(handle,CURLINFO_RESPONSE_CODE,&http);curl_easy_getinfo(handle,CURLINFO_NUM_CONNECTS,&connections);
                }
                network_done=now_ns();
                curl_easy_setopt(handle,CURLOPT_HTTPHEADER,nullptr);curl_slist_free_all(headers);headers=nullptr;
                curl_easy_setopt(handle,CURLOPT_POSTFIELDS,nullptr);curl_easy_setopt(handle,CURLOPT_POSTFIELDSIZE,0L);
                std::fill(job->body.begin(),job->body.end(),'\0');job->body.clear();
                std::fill(payload.begin(),payload.end(),'\0');payload.clear();
                bool journal=authority.outcome(job,status==CURLE_OK && http==200);
                std::ostringstream result;result<<"DONE "<<static_cast<int>(status)<<' '<<http<<' '<<wake<<' '<<request.checked<<' '<<request.sent<<' '<<network_done<<' '<<now_ns()<<' '<<connections<<' '<<job->batch->claim_finished<<' '<<journal<<' '<<request.guard_sql<<' '<<request.account_version<<' '<<request.global_version<<' '<<response.size()<<'\n';
                if(!send_all(fd,result.str()) || !send_all(fd,response) || !send_all(fd,metrics(request)))break;
            }catch(const std::exception &e){if(!send_all(fd,std::string("ERROR ")+e.what()+"\n"))break;}
        }
    }catch(const std::exception &e){send_all(fd,std::string("ERROR ")+e.what()+"\n");}
    if(headers)curl_slist_free_all(headers);
    if(handle)curl_easy_cleanup(handle);
}

int main(int argc,char **argv){
    umask(077);
    try{
        if(argc!=8)throw std::runtime_error("usage: executor socket school|fixture port ca private_state workers init|open");
        const pid_t parent=getppid();if(parent<=1)throw std::runtime_error("parent unavailable");
        std::thread([parent]{for(;;){std::this_thread::sleep_for(std::chrono::milliseconds(50));if(getppid()!=parent)_exit(89);}}).detach();
        std::string path=argv[1],endpoint=argv[2],ca=argv[4],directory=argv[5],mode=argv[7];
        target_port=static_cast<int>(number(argv[3]));unsigned workers=static_cast<unsigned>(number(argv[6]));
        if(endpoint=="fixture"){
            fixture_endpoint=true;
            if(target_port<1024 || target_port>65535 || target_port==5003 || target_port==27019 || ca=="-")throw std::runtime_error("unsafe fixture endpoint");
            target_base="https://127.0.0.1:"+std::to_string(target_port);target_ca=ca;
        }else if(endpoint!="school" || target_port!=443 || ca!="-")throw std::runtime_error("fixed school endpoint required");
        if(workers<1 || workers>24 || path.size()>=sizeof(sockaddr_un::sun_path) || (mode!="init" && mode!="open"))throw std::runtime_error("unsafe launch config");
        native_platform::initialize_clock();options.load();timed_vfs::install();Authority authority(directory,mode=="init");
        if(curl_global_init(CURL_GLOBAL_ALL)!=CURLE_OK)throw std::runtime_error("curl init failed");
        int listener=socket(AF_UNIX,SOCK_STREAM,0);sockaddr_un address{};address.sun_family=AF_UNIX;std::strcpy(address.sun_path,path.c_str());
        if(listener<0 || bind(listener,reinterpret_cast<sockaddr *>(&address),sizeof(address)) || listen(listener,128))throw std::runtime_error("private socket unavailable");
        std::cout<<"READY "<<authority.identity<<' '<<authority.epoch<<' '<<sqlite3_libversion()<<' '<<curl_version()<<std::endl;
        PeerThreads threads;std::set<int> active;std::mutex active_mutex;
        while(!authority.stopping){
            threads.reap();pollfd polling{listener,POLLIN,0};if(poll(&polling,1,100)<=0)continue;
            int fd=accept(listener,nullptr,nullptr);if(fd<0)continue;
            // A peer reset can make Mac SO_NOSIGPIPE fail even though queued
            // input is readable. Never send or dispatch that unprotected fd.
            if(!native_platform::configure_socket(fd)){close(fd);continue;}
            {std::lock_guard lock(active_mutex);active.insert(fd);}
            threads.emplace_back([&,fd]{
                std::string role;
                if(read_line(fd,role)){
                    if(role=="CONTROL")control(fd,authority,workers);
                    else if(role.rfind("READBACK ",0)==0){try{
                        std::string id=role.substr(9);if(!token(id))throw std::runtime_error("invalid job id");
                        auto row=authority.job_status(id);
                        std::string reply=row.empty()?"NOTFOUND":"JOB "+row["id"]+" "+row["epoch"]+" "+row["session"]+" "+row["claimed"]+" "+row["status"];
                        send_all(fd,reply+"\n");
                    }catch(const std::exception &e){send_all(fd,std::string("ERROR ")+e.what()+"\n");}}
                    else if(role.rfind("RENEW ",0)==0){try{std::istringstream in(role);std::string word,boot,session,ttl;if(!(in>>word>>boot>>session>>ttl))throw std::runtime_error("bad renewal");authority.lease(number(boot),number(session),number(ttl));send_all(fd,"ACK LEASE\n");}catch(const std::exception &e){send_all(fd,std::string("ERROR ")+e.what()+"\n");}}
                    else if(role.rfind("WORKER ",0)==0){try{unsigned index=static_cast<unsigned>(number(role.substr(7)));if(index>=workers)throw std::runtime_error("worker index");worker(fd,index,authority);}catch(const std::exception &e){send_all(fd,std::string("ERROR ")+e.what()+"\n");}}
                    else send_all(fd,"ERROR invalid role\n");
                }
                {std::lock_guard lock(active_mutex);active.erase(fd);}close(fd);
            });
        }
        close(listener);{std::lock_guard lock(active_mutex);for(int fd:active)shutdown(fd,SHUT_RDWR);}
        threads.join();unlink(path.c_str());curl_global_cleanup();return 0;
    }catch(const std::exception &e){std::cerr<<e.what()<<std::endl;return 2;}
}
