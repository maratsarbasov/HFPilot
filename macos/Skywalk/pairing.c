// Explicit, bounded local setup. No dial, answer, SCO or media commands exist here.
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>
#include "hci.h"
#include "btstack_event.h"
#include "btstack_util.h"
#include "l2cap.h"
#include "classic/rfcomm.h"
#include "classic/sdp_server.h"
#include "classic/sdp_util.h"
#include "classic/hfp_hf.h"
#include "pairing.h"

static int control, finished, selected, approved, pending_ssp, slc, working;
static int active=-1, setup=-1, held=-1;
static unsigned serial, pending;
static pid_t parent;
static uint32_t last_ping, expires, ready_at;
static uint32_t failure_at, retry_at;
static int repairing, repair_attempts, slc_requested;
static uint32_t profile_at;
static unsigned profile_attempts;
static uint8_t peer_io=SSP_IO_CAPABILITY_UNKNOWN;
static bd_addr_t peer;
static hci_con_handle_t handle=HCI_CON_HANDLE_INVALID;
static uint8_t record[300];
static char name[249];

static uint32_t now(void) { return btstack_run_loop_get_time_ms(); }
static void emit(const char *message) {
    if(send(control,message,strlen(message),MSG_DONTWAIT)<0) finished=1;
}
static void error(const char *reason) {
    printf("PAIR_ERROR %s\n",reason);
    char message[128]; snprintf(message,sizeof(message),"{\"event\":\"error\",\"reason\":\"%s\"}",reason);
    emit(message); finished=1;
}
static void request_confirmation(int ssp, int compare, unsigned code) {
    printf("PAIR_CONFIRM kind=%s\n",ssp?(compare?"numeric":"just-works"):"existing-bond");
    char message[192]; pending=++serial; pending_ssp=ssp;
    if(compare) snprintf(message,sizeof(message),"{\"event\":\"confirm\",\"id\":%u,\"address\":\"%s\",\"code\":\"%06u\"}",pending,bd_addr_to_str(peer),code);
    else snprintf(message,sizeof(message),"{\"event\":\"confirm\",\"id\":%u,\"address\":\"%s\",\"code\":null}",pending,bd_addr_to_str(peer));
    emit(message); // Private socket only. Never log the comparison code.
}
static int accept_peer(bd_addr_t address,hci_link_type_t type) {
    if(finished || !working || type!=HCI_LINK_TYPE_ACL) return 0;
    if(selected) return !memcmp(address,peer,6);
    memcpy(peer,address,6); selected=1;
    // Connect Phone is an explicit fresh setup. A remembered Mac key must not
    // be tried first: iOS may already have forgotten it and fail its UI before
    // an automatic second handshake can display a comparison code there.
    // This helper only owns the staging copy, never the active credential DB.
    gap_drop_link_key_for_bd_addr(peer);
    puts("PAIR_FRESH_TARGET");
    gap_discoverable_control(0);
    char message[100]; snprintf(message,sizeof(message),"{\"event\":\"device\",\"address\":\"%s\"}",bd_addr_to_str(peer));
    emit(message);
    return 1;
}
static void hfp_event(uint8_t type,uint16_t channel,uint8_t *p,uint16_t size) {
    (void)channel;
    if(type!=HCI_EVENT_PACKET || size<3 || p[0]!=HCI_EVENT_HFP_META) return;
    switch(hci_event_hfp_meta_get_subevent_code(p)) {
    case HFP_SUBEVENT_SERVICE_LEVEL_CONNECTION_ESTABLISHED: {
        unsigned status=hfp_subevent_service_level_connection_established_get_status(p);
        printf("PAIR_HFP status=0x%02x\n",status);
        if(status==ERROR_CODE_CONNECTION_REJECTED_DUE_TO_LIMITED_RESOURCES && profile_attempts<3) {
            // iOS can reject RFCOMM while its post-pairing profile setup is
            // still in progress. Keep the new bond/ACL and retry the profile,
            // never restart SSP or ask the user to compare another code.
            slc_requested=0; profile_at=now()+1500; failure_at=0;
            puts("PAIR_HFP_RETRY"); emit("{\"event\":\"connecting\"}");
            break;
        }
        if(status) {
            if(!repairing) failure_at=now()+500;
            break;
        }
        bd_addr_t address; hfp_subevent_service_level_connection_established_get_bd_addr(p,address);
        hci_con_handle_t connection=hfp_subevent_service_level_connection_established_get_acl_handle(p);
        if(!selected || memcmp(address,peer,6)) { gap_disconnect(connection); break; }
        handle=connection; slc=1; profile_at=0;
        hfp_hf_enable_status_update_for_all_ag_indicators(handle);
        break;
    }
    case HFP_SUBEVENT_AG_INDICATOR_STATUS_CHANGED: {
        const char *indicator=hfp_subevent_ag_indicator_status_changed_get_indicator_name(p);
        int value=hfp_subevent_ag_indicator_status_changed_get_indicator_status(p);
        if(!strcmp(indicator,"call")) active=value;
        if(!strcmp(indicator,"callsetup")) setup=value;
        if(!strcmp(indicator,"callheld")) held=value;
        break;
    }
    case HFP_SUBEVENT_SERVICE_LEVEL_CONNECTION_RELEASED:
        if(!finished && !repairing && !profile_at) failure_at=now()+500;
        slc=0; break;
    default: break;
    }
}
int pairing_init(int fd) {
    control=fd; parent=getppid(); last_ping=now();
    int flags=fcntl(fd,F_GETFL);
    if(flags<0 || fcntl(fd,F_SETFL,flags|O_NONBLOCK)) return -1;
    l2cap_init(); rfcomm_init(); sdp_init(); hfp_hf_init(1);
    uint16_t features=(1<<HFP_HFSF_CLI_PRESENTATION_CAPABILITY)|(1<<HFP_HFSF_ENHANCED_CALL_STATUS)|
        (1<<HFP_HFSF_CODEC_NEGOTIATION)|(1<<HFP_HFSF_ESCO_S4);
    uint8_t codecs[]={HFP_CODEC_CVSD,HFP_CODEC_MSBC};
    hfp_hf_init_supported_features(features); hfp_hf_init_codecs(2,codecs);
    hfp_hf_register_packet_handler(hfp_event);
    hfp_hf_create_sdp_record_with_codecs(record,sdp_create_service_record_handle(),1,"HFPilot",features,2,codecs);
    sdp_register_service(record);
    gap_set_local_name("HFPilot"); gap_set_class_of_device(0x200408);
    gap_ssp_set_enable(1); gap_ssp_set_auto_accept(0);
    gap_ssp_set_io_capability(SSP_IO_CAPABILITY_DISPLAY_YES_NO);
    gap_ssp_set_authentication_requirement(SSP_IO_AUTHREQ_MITM_PROTECTION_REQUIRED_GENERAL_BONDING);
    gap_set_bondable_mode(1); gap_set_allow_role_switch(1);
    gap_register_classic_connection_filter(accept_peer);
    gap_connectable_control(0); gap_discoverable_control(0);
    return 0;
}
void pairing_working(void) {
    working=1; expires=now()+120000;
    gap_connectable_control(1); gap_discoverable_control(1);
    emit("{\"event\":\"waiting\"}");
}
void pairing_event(uint8_t type,uint8_t *p,uint16_t size) {
    if(type!=HCI_EVENT_PACKET || size<2 || size!=(uint16_t)p[1]+2 || finished) return;
    bd_addr_t address;
    switch(p[0]) {
    case HCI_EVENT_CONNECTION_COMPLETE:
        if(size<13) break;
        hci_event_connection_complete_get_bd_addr(p,address);
        if(!selected || memcmp(address,peer,6)) break;
        printf("PAIR_ACL status=0x%02x\n",p[2]);
        if(p[2]) { error("connection_failed"); break; }
        handle=hci_event_connection_complete_get_connection_handle(p);
        peer_io=SSP_IO_CAPABILITY_UNKNOWN;
        // An incoming ACL is only the transport. iOS may wait for the HF to
        // authenticate and open the profile; neither depends on remote naming.
        gap_request_security_level(handle,LEVEL_2);
        emit("{\"event\":\"securing\"}");
        gap_remote_name_request(peer,0,0);
        break;
    case HCI_EVENT_REMOTE_NAME_REQUEST_COMPLETE: {
        if(size<10 || p[2]) break;
        hci_event_remote_name_request_complete_get_bd_addr(p,address);
        if(!selected || memcmp(address,peer,6)) break;
        size_t count=size-9; if(count>248) count=248;
        memcpy(name,p+9,count); name[count]=0;
        // JSON byte escapes keep untrusted names/controls out of the protocol.
        // Encode UTF-8 as hex, decode with replacement in the local UI adapter.
        char message[560]; size_t offset=(size_t)sprintf(message,"{\"event\":\"name\",\"utf8_hex\":\"");
        for(size_t i=0;i<count && name[i];i++) offset+=(size_t)sprintf(message+offset,"%02x",(unsigned char)name[i]);
        strcpy(message+offset,"\"}"); emit(message); break;
    }
    case HCI_EVENT_IO_CAPABILITY_RESPONSE:
        if(size<11) break;
        hci_event_io_capability_response_get_bd_addr(p,address);
        if(!selected || memcmp(address,peer,6)) break;
        peer_io=hci_event_io_capability_response_get_io_capability(p);
        printf("PAIR_PEER_IO capability=0x%02x oob=0x%02x auth=0x%02x\n",peer_io,
            hci_event_io_capability_response_get_oob_data_present(p),
            hci_event_io_capability_response_get_authentication_requirements(p));
        break;
    case HCI_EVENT_USER_CONFIRMATION_REQUEST:
        if(size<12) break;
        hci_event_user_confirmation_request_get_bd_addr(p,address);
        if(!selected || memcmp(address,peer,6)) { gap_ssp_confirmation_negative(address); break; }
        // This event is also used by Just Works; its number is not always a
        // value the phone can display. Local DisplayYesNo + remote NoInputNoOutput
        // needs explicit yes/no consent here, without displaying that number.
        // Core v6.0, Vol 3, Part C, Table 5.7. Never guess for unknown IO caps.
        if(peer_io!=SSP_IO_CAPABILITY_DISPLAY_YES_NO && peer_io!=SSP_IO_CAPABILITY_DISPLAY_ONLY &&
           peer_io!=SSP_IO_CAPABILITY_NO_INPUT_NO_OUTPUT) {
            puts("PAIR_UNEXPECTED_CONFIRMATION_IO");
            gap_ssp_confirmation_negative(peer); error("authentication_failed"); break;
        }
        approved=0;
        request_confirmation(1,peer_io!=SSP_IO_CAPABILITY_NO_INPUT_NO_OUTPUT,
            hci_event_user_confirmation_request_get_numeric_value(p));
        break;
    case HCI_EVENT_PIN_CODE_REQUEST:
        if(size<8) break;
        hci_event_pin_code_request_get_bd_addr(p,address); gap_pin_code_negative(address);
        error("legacy_pairing"); break;
    case HCI_EVENT_AUTHENTICATION_COMPLETE:
        if(size<5 || hci_event_authentication_complete_get_connection_handle(p)!=handle) break;
        printf("PAIR_AUTH status=0x%02x\n",p[2]);
        if(p[2]==ERROR_CODE_PIN_OR_KEY_MISSING && !repair_attempts) {
            // The selected phone forgot its key. Clear only that peer in the
            // STAGED database, then make one fresh handshake with that same peer.
            gap_drop_link_key_for_bd_addr(peer);
            repairing=1; repair_attempts++; retry_at=now()+1000; failure_at=0;
            approved=0; pending=0; pending_ssp=0; slc=0;
            slc_requested=0;
            profile_at=0;
            peer_io=SSP_IO_CAPABILITY_UNKNOWN;
            active=setup=held=-1;
            gap_disconnect(handle);
            emit("{\"event\":\"connecting\"}");
        } else if(p[2]) error(p[2]==ERROR_CODE_PIN_OR_KEY_MISSING || p[2]==ERROR_CODE_AUTHENTICATION_FAILURE ? "forget_on_phone" : "authentication_failed");
        break;
    case HCI_EVENT_SIMPLE_PAIRING_COMPLETE:
        if(size<9) break;
        hci_event_simple_pairing_complete_get_bd_addr(p,address);
        if(selected && !memcmp(address,peer,6)) {
            printf("PAIR_SSP_COMPLETE status=0x%02x\n",p[2]);
            if(p[2]) error("pairing_declined");
        }
        break;
    case HCI_EVENT_DISCONNECTION_COMPLETE:
        if(size>=6 && hci_event_disconnection_complete_get_connection_handle(p)==handle) {
            printf("PAIR_DISCONNECTED reason=0x%02x\n",hci_event_disconnection_complete_get_reason(p));
            handle=HCI_CON_HANDLE_INVALID;
            if(!repairing) failure_at=now()+500;
        }
        break;
    default: break;
    }
}
void pairing_tick(void) {
    if(finished) return;
    if(getppid()!=parent || now()-last_ping>6000) { error("owner_lost"); return; }
    if(working && (int32_t)(now()-expires)>=0) { error("timeout"); return; }
    if(failure_at && (int32_t)(now()-failure_at)>=0) { error("connection_failed"); return; }
    char message[80]; ssize_t count;
    while((count=recv(control,message,sizeof(message)-1,MSG_DONTWAIT))>0) {
        message[count]=0;
        if(!strcmp(message,"ping")) { last_ping=now(); continue; }
        if(!strcmp(message,"cancel")) { finished=1; return; }
        unsigned id; char extra;
        if(sscanf(message,"confirm %u %c",&id,&extra)==1 && pending && id==pending) {
            approved=1; pending=0;
            if(pending_ssp) gap_ssp_confirmation_response(peer);
            pending_ssp=0; emit("{\"event\":\"connecting\"}");
        }
    }
    if(handle==HCI_CON_HANDLE_INVALID) {
        if(repairing && (int32_t)(now()-retry_at)>=0) {
            repairing=0;
            slc_requested=1;
            if(hfp_hf_establish_service_level_connection(peer)) error("forget_on_phone");
        }
        return;
    }
    if(active>0 || setup>0 || held>0) { error("phone_busy"); return; }
    if(gap_security_level(handle)<LEVEL_2) return;
    if(!approved && !pending) request_confirmation(0,0,0);
    if(!slc && !slc_requested && !profile_at) profile_at=now()+1000;
    if(!slc && !slc_requested && approved && (int32_t)(now()-profile_at)>=0) {
        slc_requested=1;
        profile_at=0; profile_attempts++;
        int result=hfp_hf_establish_service_level_connection(peer);
        printf("PAIR_HFP_START status=0x%02x\n",result);
        if(result) { error("connection_failed"); return; }
    }
    if(!approved || !slc || active!=0 || setup!=0 || held!=0 || !gap_bonded(handle)) return;
    // Give the optional remote name query a short chance to finish.
    if(!ready_at) ready_at=now();
    if(!name[0] && now()-ready_at<1000) return;
    char result[120]; snprintf(result,sizeof(result),"{\"event\":\"connected\",\"address\":\"%s\"}",bd_addr_to_str(peer));
    emit(result); finished=1;
}
void pairing_stop(void) {
    finished=1;
    gap_discoverable_control(0); gap_connectable_control(0); gap_set_bondable_mode(0);
    if(pending_ssp) gap_ssp_confirmation_negative(peer);
    if(handle!=HCI_CON_HANDLE_INVALID) gap_disconnect(handle);
}
int pairing_finished(void) { return finished; }
