#pragma once
// OS primitives only. Linux fsync does not imply macOS F_FULLFSYNC semantics.
#include <cerrno>
#include <cstddef>
#include <cstdint>
#include <fcntl.h>
#include <iomanip>
#include <sstream>
#include <stdexcept>
#include <string>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>
#if defined(__APPLE__)
#include <CommonCrypto/CommonDigest.h>
#include <mach/mach_time.h>
#include <pthread/qos.h>
#include <stdlib.h>
#elif defined(__linux__)
#include <openssl/sha.h>
#include <sys/random.h>
#else
#error "Native executor supports macOS and Linux only"
#endif

namespace native_platform {
#if defined(__APPLE__)
inline constexpr const char *name="darwin",*durability_profile="darwin_fullfsync";
inline constexpr int fullfsync=1,send_flags=0;
inline mach_timebase_info_data_t timebase{};
inline void initialize_clock(){
    if(mach_timebase_info(&timebase)!=KERN_SUCCESS || !timebase.numer || !timebase.denom)
        throw std::runtime_error("continuous clock unavailable");
}
inline uint64_t continuous_ns(){
    if(!timebase.denom)throw std::runtime_error("continuous clock not initialized");
    return static_cast<uint64_t>(static_cast<__uint128_t>(mach_continuous_time())*timebase.numer/timebase.denom);
}
inline void sleep_for_ns(uint64_t duration){
    mach_wait_until(mach_absolute_time()+static_cast<uint64_t>(static_cast<__uint128_t>(duration)*timebase.denom/timebase.numer));
}
inline void worker_priority(){pthread_set_qos_class_self_np(QOS_CLASS_USER_INITIATED,0);}
inline bool configure_socket(int fd){int yes=1;return setsockopt(fd,SOL_SOCKET,SO_NOSIGPIPE,&yes,sizeof(yes))==0;}
inline int sync_file(int fd){return fcntl(fd,F_FULLFSYNC);}
inline void random_bytes(unsigned char *target,size_t size){arc4random_buf(target,size);}
inline void sha256(const std::string &value,unsigned char (&result)[32]){
    if(!CC_SHA256(value.data(),static_cast<CC_LONG>(value.size()),result))throw std::runtime_error("SHA256 failed");
}
#else
inline constexpr const char *name="linux",*durability_profile="linux_posix_fsync";
inline constexpr int fullfsync=0,send_flags=MSG_NOSIGNAL;
inline uint64_t continuous_ns(){
    timespec value{};
    if(clock_gettime(CLOCK_BOOTTIME,&value) || value.tv_sec<0 || value.tv_nsec<0 || value.tv_nsec>=1000000000L)
        throw std::runtime_error("continuous clock unavailable");
    auto total=static_cast<__uint128_t>(value.tv_sec)*1000000000ULL+static_cast<uint64_t>(value.tv_nsec);
    if(total>UINT64_MAX)throw std::runtime_error("continuous clock overflow");
    return static_cast<uint64_t>(total);
}
inline void initialize_clock(){(void)continuous_ns();}
inline void sleep_for_ns(uint64_t duration){
    // nanosleep excludes suspend. Recheck BOOTTIME/lease/expiry every20ms.
    if(duration>20000000ULL)duration=20000000ULL;
    timespec delay{static_cast<time_t>(duration/1000000000ULL),static_cast<long>(duration%1000000000ULL)};
    if(nanosleep(&delay,nullptr) && errno!=EINTR)throw std::runtime_error("deadline wait failed");
}
inline void worker_priority(){} // No scheduler privileges or policy changes.
inline bool configure_socket(int fd){(void)fd;return true;}
inline int sync_file(int fd){int status;do{status=fsync(fd);}while(status && errno==EINTR);return status;}
inline void random_bytes(unsigned char *target,size_t size){
    size_t done=0;
    while(done<size){
        ssize_t count=getrandom(target+done,size-done,0);
        if(count<0 && errno==EINTR)continue;
        if(count<=0)throw std::runtime_error("secure randomness unavailable");
        done+=static_cast<size_t>(count);
    }
}
inline void sha256(const std::string &value,unsigned char (&result)[32]){
    if(!SHA256(reinterpret_cast<const unsigned char *>(value.data()),value.size(),result))throw std::runtime_error("SHA256 failed");
}
#endif
inline std::string hash(const std::string &value){
    unsigned char result[32];sha256(value,result);
    std::ostringstream out;out<<std::hex<<std::setfill('0');
    for(unsigned char byte:result)out<<std::setw(2)<<static_cast<unsigned>(byte);
    return out.str();
}
}
