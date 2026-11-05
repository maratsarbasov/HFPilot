// Exercise real agent_ipc.c over inherited sockets, with a simulated phone.
// No controller, pairing database, API or external call is used by this fixture.
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <time.h>
#include "phone.h"
#include "agent_ipc.h"
static phone_state state;
static phone_callbacks cb;
static int dialed_once, wideband_enabled;
static int caller_packet(const char *number,const char *name,int invalid) {
    uint8_t event[255]={0xe9,0,0x17,1,0,145};
    size_t n=strlen(number)+1, a=strlen(name)+1, size=8+n+a;
    event[1]=(uint8_t)(size-2); event[6]=(uint8_t)n;
    memcpy(event+7,number,n); event[7+n]=(uint8_t)a;
    memcpy(event+8+n,name,a);
    if(invalid) event[6]=250; // Deliberately inconsistent length, no overread.
    return phone_caller_receive(&state,event,size);
}
static uint64_t now_us(void) {
    struct timespec ts; clock_gettime(CLOCK_MONOTONIC,&ts);
    return (uint64_t)ts.tv_sec*1000000+(uint64_t)ts.tv_nsec/1000;
}
int phone_init(const char *address,const phone_callbacks *callbacks, int pairing, int wideband) {
    (void)address; (void)pairing; cb=*callbacks; wideband_enabled=wideband;
    state.codec=1; state.sample_rate=8000; return 0;
}
const phone_state *phone_get_state(void) { return &state; }
int phone_command(const char *cmd,unsigned generation) {
    if(!strcmp(cmd,"connect")) state.slc=1;
    else if(!strcmp(cmd,"select-msbc") && wideband_enabled) { state.codec=2; state.sample_rate=16000; }
    else if(!strcmp(cmd,"select-cvsd")) { state.codec=1; state.sample_rate=8000; }
    else if(!strcmp(cmd,"ring")) {
        phone_indicator(&state,"callsetup",1);
    }
    else if(!strcmp(cmd,"ring-early")) phone_caller_ring(&state);
    else if(!strcmp(cmd,"sco-off")) state.sco=0;
    else if(!strcmp(cmd,"sco-on")) state.sco=1;
    else if(generation!=state.generation) return -1;
    else if(!strcmp(cmd,"dial-progress") && state.outgoing) phone_indicator(&state,"callsetup",2);
    else if(!strcmp(cmd,"dial-alert") && state.outgoing) phone_indicator(&state,"callsetup",3);
    else if(!strcmp(cmd,"dial-answer") && state.outgoing) {
        phone_indicator(&state,"call",1); phone_indicator(&state,"callsetup",0); state.sco=1;
    }
    else if(!strcmp(cmd,"remote-end")) {
        phone_indicator(&state,"call",0); phone_indicator(&state,"callsetup",0); state.sco=0;
    }
    else if(!strcmp(cmd,"caller-query")) {
        uint8_t event[]={0xe9,0,0x18,1,0,1,1,4,0,0,145,'+','1','2','0','2','5','5','5','0','1','9','9',0};
        if(phone_caller_parse_clcc(&state.caller,event,sizeof(event))) return -1;
    }
    else if(!strcmp(cmd,"caller") || !strcmp(cmd,"caller-invalid")) {
        if(caller_packet("+12025550123","Clinic \"A\"\\Desk\nТест",!strcmp(cmd,"caller-invalid"))) return -1;
    } else if(!strcmp(cmd,"caller-empty")) {
        if(caller_packet("","",0)) return -1;
    }
    else if(!strcmp(cmd,"answer") && state.setup==1) {
        state.setup=0; state.active=1; state.accepted=1; state.sco=1;
    } else if(!strcmp(cmd,"hangup") && phone_can_hangup(&state)) {
        state.active=state.setup=state.accepted=state.outgoing=state.dial_pending=state.sco=0;
    } else return -1;
    cb.state(&state); return 0;
}
int phone_dial(const char *number,unsigned generation) {
    if(dialed_once || !phone_number_valid(number) || phone_reserve_outgoing(&state,generation)) return -1;
    dialed_once=1; cb.state(&state); return 0;
}
void phone_stop(void) {
    if(phone_can_hangup(&state)) {
        state.active=state.setup=state.accepted=state.outgoing=state.dial_pending=state.sco=0;
    }
    cb.state(&state);
}
int main(int argc,char **argv) {
    if(argc!=9 || strcmp(argv[7],"v3") || agent_init(argv[3],atoi(argv[4]),atoi(argv[5]),!strcmp(argv[8],"auto"))) return 2;
    agent_working();
    uint64_t next_audio=now_us();
    while(!agent_finished()) {
        agent_tick();
        uint64_t now=now_us();
        while(now>=next_audio) {
            next_audio+=10000;
            if(!state.active || !state.sco) continue;
            // 10 ms chunks. Playback is echoed into the receive path to test
            // both real IPC queues and both streaming resamplers end-to-end.
            uint8_t packet[320]={0};
            size_t n=state.sample_rate/50;
            cb.playback(packet,n);
            cb.sent(packet,n);
            cb.received(packet,n,0);
        }
        usleep(1000);
    }
    phone_stop(); agent_close(); return 0;
}
