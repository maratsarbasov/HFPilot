// Call ownership is separate from audio and survives the outgoing setup phase.
#include <limits.h>
#include <string.h>
#include "phone.h"

int phone_number_valid(const char *number) {
    size_t n=strlen(number);
    if(n<8 || n>16 || number[0]!='+' || number[1]<'1' || number[1]>'9') return 0;
    for(size_t i=2;i<n;i++) if(number[i]<'0' || number[i]>'9') return 0;
    return 1;
}

int phone_reserve_outgoing(phone_state *s,unsigned generation) {
    if(!s->slc || s->active!=0 || s->setup!=0 || s->held!=0 || s->accepted ||
       s->ring_seen || s->generation==UINT_MAX || generation!=s->generation+1) return -1;
    s->generation=generation;
    s->accepted=s->outgoing=s->dial_pending=1;
    phone_caller_clear(s);
    return 0;
}

static void release(phone_state *s) { s->accepted=s->outgoing=s->dial_pending=0; }

void phone_indicator(phone_state *s,const char *name,int value) {
    if(!strcmp(name,"call")) {
        if(s->active==1 && value==0) release(s);
        if(value==1) {
            // An active call appearing without our answer/dial belongs to someone else.
            if(s->active==0 && s->setup==0 && !s->accepted) s->generation++;
            s->dial_pending=0;
        }
        s->active=value;
    } else if(!strcmp(name,"callsetup")) {
        int previous=s->setup;
        if(value && previous<=0 && s->active==0 && s->held==0) {
            if(s->accepted && s->outgoing && s->dial_pending && (value==2 || value==3)) {
                s->dial_pending=0; // Same generation reserved before sending ATD.
            } else {
                s->generation++;
                release(s);
                phone_caller_begin(s,value==1);
            }
        }
        s->setup=value;
        if(value==0 && s->active==0) {
            if(previous>0) release(s); // Busy, rejected or cancelled before answer.
            phone_caller_clear(s);
        }
    } else if(!strcmp(name,"callheld")) s->held=value;
}

int phone_can_hangup(const phone_state *s) {
    if(!s->slc || !s->accepted || s->held!=0 || s->setup==1) return 0;
    if(s->active==1 && s->setup==0) return 1;
    return s->outgoing && (s->dial_pending || s->setup==2 || s->setup==3);
}
