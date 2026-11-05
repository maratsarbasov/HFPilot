// Exercise the actual pairing event/approval gate with no controller transport.
#include <assert.h>
#include <stdio.h>
#include <sys/socket.h>
#include "hci.h"
#include "btstack_event.h"
#include "btstack_util.h"
#include "l2cap.h"
#include "classic/rfcomm.h"
#include "classic/sdp_server.h"
#include "classic/sdp_util.h"
#include "classic/hfp_hf.h"
static uint32_t clock_ms=100;
static int accepted_count, rejected_count, disconnected_count, bonded_value, dropped_count, retried_count;
static int security_requests, name_requests;
static gap_security_level_t level=LEVEL_0;
static uint32_t test_now(void) { return clock_ms; }
static void scan(uint8_t value) { (void)value; }
static int accept_confirmation(const bd_addr_t address) { (void)address; accepted_count++; return 0; }
static int reject_confirmation(const bd_addr_t address) { (void)address; rejected_count++; return 0; }
static uint8_t disconnect_peer(hci_con_handle_t connection) { (void)connection; disconnected_count++; return 0; }
static gap_security_level_t security(hci_con_handle_t connection) { (void)connection; return level; }
static bool bonded(hci_con_handle_t connection) { (void)connection; return bonded_value; }
static void drop_key(bd_addr_t address) { (void)address; dropped_count++; }
static uint8_t reconnect(bd_addr_t address) { (void)address; retried_count++; return 0; }
static void request_security(hci_con_handle_t connection,gap_security_level_t requested) {
    assert(connection==1 && requested==LEVEL_2); security_requests++;
}
static int remote_name(const bd_addr_t address,uint8_t scan_mode,uint16_t offset) {
    (void)address; (void)scan_mode; (void)offset; name_requests++; return 0;
}
#define btstack_run_loop_get_time_ms test_now
#define gap_discoverable_control scan
#define gap_connectable_control scan
#define gap_set_bondable_mode scan
#define gap_ssp_confirmation_response accept_confirmation
#define gap_ssp_confirmation_negative reject_confirmation
#define gap_disconnect disconnect_peer
#define gap_security_level security
#define gap_bonded bonded
#define gap_drop_link_key_for_bd_addr drop_key
#define hfp_hf_establish_service_level_connection reconnect
#define gap_request_security_level request_security
#define gap_remote_name_request remote_name
#include "../../macos/Skywalk/pairing.c"

static void drain(int fd) {
    char buffer[1024];
    while(recv(fd,buffer,sizeof(buffer),MSG_DONTWAIT)>0) {}
}
int main(void) {
    int sockets[2]; assert(socketpair(AF_UNIX,SOCK_DGRAM,0,sockets)==0);
    control=sockets[0]; fcntl(control,F_SETFL,O_NONBLOCK);
    parent=getppid(); last_ping=clock_ms; expires=clock_ms+120000; working=1;
    bd_addr_t a={1,2,3,4,5,6}, b={6,5,4,3,2,1};
    assert(accept_peer(a,HCI_LINK_TYPE_ACL));
    assert(dropped_count==1); // Fresh setup before the first authentication request.
    assert(accept_peer(a,HCI_LINK_TYPE_ACL));
    assert(dropped_count==1); // Repeated transport requests do not invalidate a new key.
    assert(!accept_peer(b,HCI_LINK_TYPE_ACL));
    assert(!accept_peer(a,HCI_LINK_TYPE_SCO));
    uint8_t connected_acl[13]={HCI_EVENT_CONNECTION_COMPLETE,11,0,1,0};
    reverse_bd_addr(a,connected_acl+5); connected_acl[11]=HCI_LINK_TYPE_ACL;
    pairing_event(HCI_EVENT_PACKET,connected_acl,sizeof(connected_acl));
    assert(security_requests==1 && name_requests==1 && handle==1);
    uint8_t io[11]={HCI_EVENT_IO_CAPABILITY_RESPONSE,9};
    reverse_bd_addr(a,io+2); io[8]=SSP_IO_CAPABILITY_DISPLAY_YES_NO;
    pairing_event(HCI_EVENT_PACKET,io,sizeof(io));
    assert(peer_io==SSP_IO_CAPABILITY_DISPLAY_YES_NO);
    uint8_t request[12]={HCI_EVENT_USER_CONFIRMATION_REQUEST,10};
    reverse_bd_addr(b,request+2);
    pairing_event(HCI_EVENT_PACKET,request,sizeof(request));
    assert(rejected_count==1 && accepted_count==0);
    reverse_bd_addr(a,request+2); little_endian_store_32(request,8,123456);
    drain(sockets[1]);
    pairing_event(HCI_EVENT_PACKET,request,sizeof(request));
    assert(pending && !approved);
    char event[256]={0}; assert(recv(sockets[1],event,sizeof(event)-1,0)>0);
    assert(strstr(event,"\"code\":\"123456\""));
    char command[64]; snprintf(command,sizeof(command),"confirm %u",pending+1);
    send(sockets[1],command,strlen(command),0); pairing_tick();
    assert(!approved && accepted_count==0);
    snprintf(command,sizeof(command),"confirm %u",pending);
    send(sockets[1],command,strlen(command),0); pairing_tick();
    assert(approved && accepted_count==1 && pending==0);
    send(sockets[1],command,strlen(command),0); pairing_tick();
    assert(accepted_count==1); // A replay cannot approve another handshake.
    active=setup=held=0;
    pairing_tick(); assert(!finished); // Not encrypted.
    // The phone sends no name response and never opens HFP. We must initiate it.
    level=LEVEL_2; pairing_tick(); assert(!finished && retried_count==0 && profile_at);
    clock_ms+=1000; pairing_tick(); assert(!finished && retried_count==1 && slc_requested);
    pairing_tick(); assert(retried_count==1); // No duplicate profile requests.
    uint8_t hfp_refused[12]={HCI_EVENT_HFP_META,10,HFP_SUBEVENT_SERVICE_LEVEL_CONNECTION_ESTABLISHED,0,0,
        ERROR_CODE_CONNECTION_REJECTED_DUE_TO_LIMITED_RESOURCES};
    reverse_bd_addr(a,hfp_refused+6);
    hfp_event(HCI_EVENT_PACKET,0,hfp_refused,sizeof(hfp_refused));
    assert(!finished && profile_at && !slc_requested);
    pairing_tick(); assert(retried_count==1); // Backoff, with the bond and consent retained.
    clock_ms+=1500; pairing_tick(); assert(retried_count==2 && approved && accepted_count==1);
    slc=1; strcpy(name,"Test Phone"); pairing_tick(); assert(!finished); // No persisted bond.
    bonded_value=1; drain(sockets[1]); pairing_tick(); assert(finished);
    memset(event,0,sizeof(event)); assert(recv(sockets[1],event,sizeof(event)-1,0)>0);
    assert(strstr(event,"\"connected\""));
    pairing_stop(); assert(disconnected_count==1);
    finished=0; active=1; pairing_tick(); assert(finished); // Never take over a call.
    finished=0; active=0; failure_at=clock_ms+500;
    uint8_t missing[5]={HCI_EVENT_AUTHENTICATION_COMPLETE,3,ERROR_CODE_PIN_OR_KEY_MISSING,1,0};
    pairing_event(HCI_EVENT_PACKET,missing,sizeof(missing));
    assert(repairing && dropped_count==2 && !finished && !failure_at);
    handle=HCI_CON_HANDLE_INVALID; clock_ms+=1100; pairing_tick();
    assert(retried_count==3 && !repairing);
    handle=1; pairing_event(HCI_EVENT_PACKET,missing,sizeof(missing));
    assert(finished && dropped_count==2); // No endless re-pair loop.
    finished=0; clock_ms+=7000; pairing_tick(); assert(finished); // Owner heartbeat.
    // Just Works still needs explicit local consent and the SSP positive reply,
    // but never presents an uncomparable number. Foreign IO cannot change it.
    finished=0; failure_at=0; last_ping=clock_ms; handle=HCI_CON_HANDLE_INVALID;
    io[8]=SSP_IO_CAPABILITY_NO_INPUT_NO_OUTPUT;
    pairing_event(HCI_EVENT_PACKET,io,sizeof(io));
    reverse_bd_addr(b,io+2); io[8]=SSP_IO_CAPABILITY_DISPLAY_YES_NO;
    pairing_event(HCI_EVENT_PACKET,io,sizeof(io));
    assert(peer_io==SSP_IO_CAPABILITY_NO_INPUT_NO_OUTPUT);
    drain(sockets[1]); pairing_event(HCI_EVENT_PACKET,request,sizeof(request));
    memset(event,0,sizeof(event)); assert(recv(sockets[1],event,sizeof(event)-1,0)>0);
    assert(strstr(event,"\"code\":null") && !strstr(event,"123456"));
    assert(!approved && pending_ssp && accepted_count==1);
    snprintf(command,sizeof(command),"confirm %u",pending);
    send(sockets[1],command,strlen(command),0); pairing_tick();
    assert(approved && accepted_count==2 && pending==0);
    peer_io=SSP_IO_CAPABILITY_UNKNOWN;
    pairing_event(HCI_EVENT_PACKET,request,sizeof(request));
    assert(finished && rejected_count==2 && accepted_count==2);
    close(sockets[0]); close(sockets[1]);
    puts("pairing approval, peer isolation, encryption, bond, busy and watchdog gates passed");
    return 0;
}
