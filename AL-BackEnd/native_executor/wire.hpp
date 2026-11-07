#pragma once
#include <charconv>
#include <cstdlib>
#include <set>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

struct SchoolRequest {std::vector<std::pair<std::string,std::string>> headers;std::string body;};
inline SchoolRequest school_request(const std::string &frame){
    const std::string magic="NJFU-CAMPUS/1\n";
    if(frame.size()>16384 || frame.compare(0,magic.size(),magic)!=0)throw std::runtime_error("invalid campus capsule");
    size_t offset=magic.size();
    auto length_line=[&](){
        size_t end=frame.find('\n',offset);
        if(end==std::string::npos || end-offset>64)throw std::runtime_error("invalid campus frame length");
        std::string line=frame.substr(offset,end-offset);offset=end+1;
        size_t split=line.find(' ');if(split==std::string::npos)throw std::runtime_error("invalid campus length pair");
        auto value=[&](size_t begin,size_t finish){
            size_t result=0;auto parsed=std::from_chars(line.data()+begin,line.data()+finish,result);
            if(begin==finish || parsed.ec!=std::errc{} || parsed.ptr!=line.data()+finish)throw std::runtime_error("invalid campus length");
            return result;
        };
        return std::make_pair(value(0,split),value(split+1,line.size()));
    };
    auto [count,body_size]=length_line();
    if(count!=5 || body_size<1 || body_size>8192)throw std::runtime_error("invalid campus field count");
    const std::set<std::string> allowed={"user-agent","accept","content-type","cookie","token"};
    std::set<std::string> names;SchoolRequest result;
    for(size_t i=0;i<count;++i){
        auto [name_size,value_size]=length_line();
        if(name_size<1 || name_size>32 || value_size<1 || value_size>4096 || name_size+value_size>frame.size()-offset)
            throw std::runtime_error("invalid campus header length");
        auto name=frame.substr(offset,name_size);offset+=name_size;
        auto value=frame.substr(offset,value_size);offset+=value_size;
        if(!allowed.count(name) || !names.insert(name).second)throw std::runtime_error("invalid campus header name");
        for(unsigned char c:value)if(c<32 || c>126)throw std::runtime_error("invalid campus header value");
        if(name=="content-type" && value!="application/json")throw std::runtime_error("invalid campus content type");
        result.headers.emplace_back(std::move(name),std::move(value));
    }
    if(frame.size()-offset!=body_size)throw std::runtime_error("invalid campus body length");
    result.body=frame.substr(offset);return result;
}
