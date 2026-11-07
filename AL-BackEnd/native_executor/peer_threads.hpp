#pragma once
#include <atomic>
#include <list>
#include <memory>
#include <thread>
#include <utility>

// The original per-peer threading model, with completed peers joined during
// service life. Live control/worker peers are never joined by reap().
class PeerThreads {
    struct Entry {std::shared_ptr<std::atomic<bool>> finished;std::thread thread;};
    std::list<Entry> entries;
public:
    PeerThreads()=default;
    PeerThreads(const PeerThreads &)=delete;
    PeerThreads &operator=(const PeerThreads &)=delete;
    template<class Action> void emplace_back(Action action){
        auto finished=std::make_shared<std::atomic<bool>>(false);
        entries.emplace_back(); // Main thread uniquely owns the thread handle.
        entries.back().finished=finished;
        try{
            entries.back().thread=std::thread([finished,action=std::move(action)]() mutable {
                action();
                finished->store(true,std::memory_order_release);
            });
        }catch(...){entries.pop_back();throw;}
    }
    void reap(){
        for(auto it=entries.begin();it!=entries.end();){
            if(it->finished->load(std::memory_order_acquire)){
                it->thread.join();it=entries.erase(it);
            }else ++it;
        }
    }
    void join(){
        for(auto &entry:entries)if(entry.thread.joinable())entry.thread.join();
        entries.clear();
    }
};
