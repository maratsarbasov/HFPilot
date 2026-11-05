#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/stat.h>
#include <fcntl.h>
#include "hci.h"
#include "hci_cmd.h"
#include "btstack_event.h"
#include "btstack_memory.h"
#include "btstack_util.h"
#include "btstack_run_loop_posix.h"
#include "classic/btstack_link_key_db_memory.h"
#include "classic/btstack_link_key_db_tlv.h"
#include "btstack_tlv_posix.h"
#include "phone.h"
#include "agent_ipc.h"
#include "pairing.h"

const hci_transport_t *skywalk_transport_instance(void);
static volatile sig_atomic_t stopping;
static btstack_timer_source_t deadline_timer;
static btstack_packet_callback_registration_t registration;
static uint32_t deadline_ms;
static int shutting_down, agent_mode, pair_mode, phone_enabled, deadline_active;
static btstack_tlv_posix_t key_store;
static int persistent_keys;
static int open_key_store(const char *path) {
    int fd=open(path,O_RDWR|O_CREAT|O_NOFOLLOW,0600);
    if(fd<0) return -1;
    struct stat info;
    int valid=fstat(fd,&info)==0 && S_ISREG(info.st_mode) && info.st_uid==getuid() && !(info.st_mode&0077);
    close(fd); if(!valid) return -1;
    const btstack_tlv_t *impl=btstack_tlv_posix_init_instance(&key_store,path);
    if(!impl || !key_store.file) return -1;
    hci_set_link_key_db(btstack_link_key_db_tlv_get_instance(impl,&key_store));
    persistent_keys=1; return 0;
}
static void signal_stop(int value) { (void)value; stopping=1; }
static void tick(btstack_timer_source_t *timer) {
    if(!shutting_down) { if(pair_mode) pairing_tick(); else agent_tick(); }
    int expired=deadline_active && (int32_t)(btstack_run_loop_get_time_ms()-deadline_ms)>=0;
    if(stopping || (!shutting_down && (pair_mode ? pairing_finished() : agent_finished())) || expired) {
        if(!shutting_down) {
            shutting_down=1; puts("STOP_REQUEST"); if(pair_mode) pairing_stop(); else agent_stop();
            deadline_ms=btstack_run_loop_get_time_ms()+3000; deadline_active=1; stopping=0;
        } else if(shutting_down==1) {
            shutting_down=2; hci_power_control(HCI_POWER_OFF);
            deadline_ms=btstack_run_loop_get_time_ms()+3000;
        } else { skywalk_transport_instance()->close(); btstack_run_loop_trigger_exit(); return; }
    }
    btstack_run_loop_set_timer(timer,agent_mode?5:100); btstack_run_loop_add_timer(timer);
}
static void event_handler(uint8_t type,uint16_t channel,uint8_t *packet,uint16_t size) {
    (void)channel;
    if(pair_mode && !shutting_down) pairing_event(type,packet,size);
    if(phone_enabled) phone_hci_event(type,channel,packet,size);
    if(type!=HCI_EVENT_PACKET || size<2) return;
    switch(packet[0]) {
    case BTSTACK_EVENT_STATE:
        printf("STACK_STATE %u\n",btstack_event_state_get_state(packet));
        if(btstack_event_state_get_state(packet)==HCI_STATE_WORKING) {
            bd_addr_t address; gap_local_bd_addr(address); printf("STACK_WORKING %s\n",bd_addr_to_str(address));
            if(agent_mode) agent_working();
            if(pair_mode) pairing_working();
        }
        if(shutting_down && btstack_event_state_get_state(packet)==HCI_STATE_OFF) btstack_run_loop_trigger_exit();
        break;
    case HCI_EVENT_COMMAND_COMPLETE:
        if(size>=6) printf("HCI_COMPLETE opcode=0x%04x status=0x%02x\n",little_endian_read_16(packet,3),packet[5]);
        if(size>=13 && little_endian_read_16(packet,3)==0x1005 && packet[5]==0)
            printf("HCI_BUFFERS acl_payload=%u sco_payload=%u acl_packets=%u sco_packets=%u\n",
                little_endian_read_16(packet,6),packet[8],little_endian_read_16(packet,9),little_endian_read_16(packet,11));
        break;
    case HCI_EVENT_COMMAND_STATUS:
        if(size>=6) printf("HCI_STATUS opcode=0x%04x status=0x%02x\n",little_endian_read_16(packet,4),packet[2]);
        break;
    default: break;
    }
}
int main(int argc,char **argv) {
    agent_mode=argc==9 && !strcmp(argv[1],"--agent");
    pair_mode=argc==5 && !strcmp(argv[1],"--pair");
    if(!agent_mode && !pair_mode) {
        fprintf(stderr,"Usage: skywalk-hfp --agent TIMEOUT PHONE CONTROL_FD AUDIO_FD KEY_DB v3 auto|cvsd\n"
                       "       skywalk-hfp --pair TIMEOUT CONTROL_FD STAGED_KEY_DB\n"); return 2;
    }
    const char *seconds=argv[2];
    char *end=NULL; long timeout=strtol(seconds,&end,10);
    if(end==seconds || *end || timeout<(agent_mode?0:1) || timeout>(agent_mode?86400:600)) {
        fprintf(stderr,"Invalid timeout. Agent TIMEOUT=0 follows the parent process lifetime and heartbeat.\n"); return 2;
    }
    if(agent_mode && (strcmp(argv[7],"v3") || (strcmp(argv[8],"auto") && strcmp(argv[8],"cvsd")))) return 2;
    setvbuf(stdout,NULL,_IOLBF,0);
    umask(0077);
    signal(SIGPIPE,SIG_IGN);
    signal(SIGTERM,signal_stop); signal(SIGINT,signal_stop);
    btstack_memory_init(); btstack_run_loop_init(btstack_run_loop_posix_get_instance());
    hci_init(skywalk_transport_instance(),NULL);
    hci_set_link_key_db(btstack_link_key_db_memory_instance());
    if(open_key_store(argv[pair_mode?4:6])) { fprintf(stderr,"Cannot open private key store (regular file, owned by current user, mode 600 required)\n"); hci_close(); return 2; }
    registration.callback=event_handler; hci_add_event_handler(&registration);
    // Only explicit setup may enable discovery/pairing.
    gap_discoverable_control(0); gap_connectable_control(0); gap_set_bondable_mode(0);
    phone_enabled=agent_mode;
    int initialized=pair_mode ? pairing_init(atoi(argv[3])) : agent_init(argv[3],atoi(argv[4]),atoi(argv[5]),!strcmp(argv[8],"auto"));
    if(initialized) { fprintf(stderr,"Invalid client arguments/files\n"); hci_close(); if(persistent_keys) btstack_tlv_posix_deinit(&key_store); return 2; }
    deadline_ms=btstack_run_loop_get_time_ms()+(uint32_t)timeout*1000;
    deadline_active=timeout>0;
    btstack_run_loop_set_timer_handler(&deadline_timer,tick);
    btstack_run_loop_set_timer(&deadline_timer,100); btstack_run_loop_add_timer(&deadline_timer);
    int result=hci_power_control(HCI_POWER_ON);
    printf("POWER_ON_RESULT %d\n",result);
    if(result==0) btstack_run_loop_execute();
    skywalk_transport_instance()->close(); if(agent_mode) agent_close(); hci_close();
    if(persistent_keys) btstack_tlv_posix_deinit(&key_store);
    puts("STOPPED"); return result ? 1:0;
}
