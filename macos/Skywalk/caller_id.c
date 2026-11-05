#include <string.h>
#include "phone.h"

int phone_caller_parse_clip(phone_caller *out, const uint8_t *p, size_t size) {
    // BTstack: header(3), ACL(2), type(1), number length(1), number including
    // NUL, alpha length(1), alpha including NUL. Validate before using offsets.
    if(size<10 || (size_t)p[1]+2!=size) return -1;
    size_t number_len=p[6];
    if(!number_len || number_len>sizeof(out->number) || 7+number_len>=size) return -1;
    size_t name_len=p[7+number_len];
    if(!name_len || name_len>sizeof(out->name) || 8+number_len+name_len!=size) return -1;
    if(p[6+number_len] || p[size-1]) return -1;
    // Embedded NULs would silently change the identity reported by the phone.
    if(memchr(p+7,0,number_len-1) || memchr(p+8+number_len,0,name_len-1)) return -1;
    phone_caller value={.received=1,.type=p[5],.source=1};
    memcpy(value.number,p+7,number_len);
    memcpy(value.name,p+8+number_len,name_len);
    *out=value;
    return 0;
}

int phone_caller_parse_clcc(phone_caller *out,const uint8_t *p,size_t size) {
    // Only the incoming (4) or accepted active (0) single voice call. The ACL
    // and requested generation are checked by phone.c. Use the callback size:
    // the pinned BTstack CLCC emitter's legacy length byte is off by one.
    if(size<12 || p[6]!=1 || (p[7]!=4 && p[7]!=0) || p[8]!=0 || p[9]!=0) return -1;
    const uint8_t *end=memchr(p+11,0,size-11);
    if(!end || (size_t)(end-(p+11))>=sizeof(out->number)) return -1;
    // Prefer explicit CLIP; never replace its name/number with less rich data.
    if(out->source==1 && (out->number[0] || out->name[0])) return 0;
    phone_caller value={.received=1,.type=p[10],.source=2};
    memcpy(value.number,p+11,(size_t)(end-(p+11)));
    *out=value;
    return 0;
}

void phone_caller_clear(phone_state *s) {
    memset(&s->caller,0,sizeof(s->caller));
    memset(&s->pending_caller,0,sizeof(s->pending_caller));
    s->ring_seen=0;
}

void phone_caller_ring(phone_state *s) {
    if(s->active==0 && s->held==0 && s->setup<=0) {
        // A fresh RING cannot reuse an unanswered older ring's identity.
        memset(&s->pending_caller,0,sizeof(s->pending_caller));
        s->ring_seen=1;
    }
}

void phone_caller_begin(phone_state *s,int incoming) {
    phone_caller pending=s->pending_caller;
    int use_pending=incoming && s->ring_seen;
    phone_caller_clear(s);
    if(use_pending) s->caller=pending;
}

int phone_caller_receive(phone_state *s,const uint8_t *p,size_t size) {
    if(s->active!=0 || s->held!=0) return 1;
    if(s->setup==1) return phone_caller_parse_clip(&s->caller,p,size);
    // iOS may send RING, CLIP, then callsetup=1. Keep early facts private until
    // that incoming generation exists. Outgoing/held/waiting calls are ignored.
    if(s->setup<=0 && s->ring_seen)
        return phone_caller_parse_clip(&s->pending_caller,p,size);
    return 1;
}
