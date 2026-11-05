// One explicitly selected phone. Transport-independent HFP state and commands.
#include <stdio.h>
#include <string.h>
#include <unistd.h>
#include "hci.h"
#include "hci_cmd.h"
#include "btstack_event.h"
#include "btstack_util.h"
#include "l2cap.h"
#include "classic/rfcomm.h"
#include "classic/sdp_server.h"
#include "classic/sdp_util.h"
#include "classic/hfp_hf.h"
#include "phone.h"
#include "sco_codec.h"

static bd_addr_t phone;
static hci_con_handle_t acl=HCI_CON_HANDLE_INVALID, sco=HCI_CON_HANDLE_INVALID;
static phone_state state = {.active=-1, .setup=-1, .held=-1};
static phone_callbacks client;
static int answered_once, allow_pairing;
static unsigned caller_query_generation;
static uint8_t sdp_record[300];
static void changed(void) {
    state.slc = acl != HCI_CON_HANDLE_INVALID;
    state.sco = sco != HCI_CON_HANDLE_INVALID;
    if(client.state) client.state(&state);
}
const phone_state *phone_get_state(void) {
    if(state.codec) sco_codec_stats(&state);
    return &state;
}
static void pcm_received(const uint8_t *data,size_t n,int bad) {
    if(state.accepted && state.active==1 && client.received) client.received(data,n,bad);
}
static void pcm_playback(uint8_t *data,size_t n) {
    if(state.accepted && state.active==1 && client.playback) client.playback(data,n);
}
static void pcm_sent(const uint8_t *data,size_t n) {
    if(state.accepted && state.active==1 && client.sent) client.sent(data,n);
}
static void audio_failed(void) {
    state.audio_error=1; changed(); hfp_hf_release_audio_connection(acl);
}
static int accept_connection(bd_addr_t address,hci_link_type_t type) {
    (void)type;
    return !memcmp(address,phone,sizeof(phone));
}
static void send_sco(void) {
    if(sco==HCI_CON_HANDLE_INVALID) return;
    int len=hci_get_sco_packet_length_for_connection(sco);
    if(len<4 || len>258) return;
    hci_reserve_packet_buffer(); uint8_t *packet=hci_get_outgoing_packet_buffer();
    little_endian_store_16(packet,0,sco); packet[2]=len-3;
    if(sco_codec_prepare(packet+3,len-3)) {
        hci_release_packet_buffer(); puts("SCO_CODEC_ENCODE_FAILED"); audio_failed(); return;
    }
    uint8_t status=hci_send_sco_packet_buffer(len);
    if(status) { printf("SCO_SEND_FAILED status=%u\n",status); audio_failed(); return; }
    sco_codec_sent();
    hci_request_sco_can_send_now_event_for_con_handle(sco);
}
static void hf_event(uint8_t type,uint16_t channel,uint8_t *p,uint16_t size) {
    (void)channel; if(type!=HCI_EVENT_PACKET || size<3 || p[0]!=HCI_EVENT_HFP_META) return;
    uint8_t status;
    switch(hci_event_hfp_meta_get_subevent_code(p)) {
    case HFP_SUBEVENT_SERVICE_LEVEL_CONNECTION_ESTABLISHED: {
        status=hfp_subevent_service_level_connection_established_get_status(p);
        printf("HFP_CONNECTED status=0x%02x\n",status);
        if(status) break;
        bd_addr_t address; hfp_subevent_service_level_connection_established_get_bd_addr(p,address);
        hci_con_handle_t handle=hfp_subevent_service_level_connection_established_get_acl_handle(p);
        if(memcmp(address,phone,6)) { hfp_hf_release_service_level_connection(handle); break; }
        acl=handle;
        phone_caller_clear(&state);
        hfp_hf_enable_status_update_for_all_ag_indicators(acl);
        printf("CALLER_ID_ENABLE status=%u\n",hfp_hf_activate_calling_line_notification(acl));
        changed(); break;
    }
    case HFP_SUBEVENT_SERVICE_LEVEL_CONNECTION_RELEASED:
        acl=HCI_CON_HANDLE_INVALID; sco=HCI_CON_HANDLE_INVALID;
        state.active=state.setup=state.held=-1;
        state.accepted=state.outgoing=state.dial_pending=0;
        phone_caller_clear(&state);
        puts("HFP_DISCONNECTED"); changed(); break;
    case HFP_SUBEVENT_AUDIO_CONNECTION_ESTABLISHED:
        status=hfp_subevent_audio_connection_established_get_status(p);
        printf("AUDIO_CONNECTED status=0x%02x codec=%u\n",status,hfp_subevent_audio_connection_established_get_negotiated_codec(p));
        if(!status) {
            sco=hfp_subevent_audio_connection_established_get_sco_handle(p);
            unsigned codec=hfp_subevent_audio_connection_established_get_negotiated_codec(p);
            phone_callbacks media={.received=pcm_received,.playback=pcm_playback,.sent=pcm_sent};
            if(sco_codec_start(codec,&media)) { audio_failed(); break; }
            sco_codec_stats(&state);
            printf("AUDIO_FORMAT codec=%s rate=%u\n",codec==2?"mSBC":"CVSD",state.sample_rate);
            changed();
            hci_request_sco_can_send_now_event_for_con_handle(sco);
        }
        break;
    case HFP_SUBEVENT_AUDIO_CONNECTION_RELEASED:
        sco=HCI_CON_HANDLE_INVALID; puts("AUDIO_DISCONNECTED"); changed(); break;
    case HFP_SUBEVENT_AG_INDICATOR_STATUS_CHANGED: {
        const char *name=hfp_subevent_ag_indicator_status_changed_get_indicator_name(p);
        int value=hfp_subevent_ag_indicator_status_changed_get_indicator_status(p);
        printf("INDICATOR %s=%d\n",name,value);
        phone_indicator(&state,name,value);
        changed();
        break;
    }
    case HFP_SUBEVENT_START_RINGING:
        if(size>=5 && hfp_subevent_start_ringing_get_acl_handle(p)==acl) phone_caller_ring(&state);
        puts("RINGING"); break;
    case HFP_SUBEVENT_CALLING_LINE_IDENTIFICATION_NOTIFICATION:
        // CLIP belongs to the sole incoming call, never a waiting/held call or
        // another paired phone. The next generation clears this snapshot.
        if(size>=5 && little_endian_read_16(p,3)==acl && acl!=HCI_CON_HANDLE_INVALID) {
            int result=phone_caller_receive(&state,p,size);
            printf("CALLER_ID_EVENT result=%d setup=%d active=%d\n",result,state.setup,state.active);
            if(!result) changed();
        }
        break;
    case HFP_SUBEVENT_ENHANCED_CALL_STATUS:
        if(size>=12 && little_endian_read_16(p,3)==acl && acl!=HCI_CON_HANDLE_INVALID &&
           !state.outgoing && caller_query_generation==state.generation && state.generation && state.held==0 &&
           ((state.active==0 && state.setup==1) || (state.accepted && state.active==1 && state.setup==0))) {
            int result=phone_caller_parse_clcc(&state.caller,p,size);
            printf("CALLER_QUERY_EVENT result=%d direction=%u status=%u\n",result,p[6],p[7]);
            if(!result) changed();
        }
        break;
    case HFP_SUBEVENT_CALL_ANSWERED: puts("CALL_ANSWERED"); break;
    case HFP_SUBEVENT_CALL_TERMINATED: puts("CALL_TERMINATED"); break;
    case HFP_SUBEVENT_COMPLETE: printf("HFP_COMMAND_COMPLETE status=%u\n",hfp_subevent_complete_get_status(p)); break;
    default: break;
    }
}
void phone_hci_event(uint8_t type,uint16_t channel,uint8_t *p,uint16_t size) {
    (void)channel;
    if(type==HCI_SCO_DATA_PACKET) {
        if(size<3 || (little_endian_read_16(p,0)&0x0fff)!=sco || size!=(uint16_t)(p[2]+3)) return;
        sco_codec_receive(p+3,size-3,(p[1]>>4)&3);
        return;
    }
    if(type!=HCI_EVENT_PACKET || size<2 || size!=(uint16_t)(p[1]+2)) return;
    bd_addr_t address;
    switch(p[0]) {
    case HCI_EVENT_USER_CONFIRMATION_REQUEST:
        if(size<12) break;
        hci_event_user_confirmation_request_get_bd_addr(p,address);
        // NO_INPUT_NO_OUTPUT uses Just Works; no numeric code is displayed/logged.
        if(allow_pairing && !memcmp(address,phone,6)) { puts("PAIR_JUST_WORKS_TARGET"); gap_ssp_confirmation_response(address); }
        else gap_ssp_confirmation_negative(address);
        break;
    case HCI_EVENT_PIN_CODE_REQUEST:
        if(size<8) break;
        hci_event_pin_code_request_get_bd_addr(p,address); gap_pin_code_negative(address); puts("LEGACY_PIN_REFUSED"); break;
    case HCI_EVENT_SIMPLE_PAIRING_COMPLETE:
        if(size<9) break;
        printf("PAIR_COMPLETE status=0x%02x\n",p[2]); break;
    case HCI_EVENT_AUTHENTICATION_COMPLETE:
        if(size<5) break;
        printf("AUTH_COMPLETE status=0x%02x\n",p[2]); break;
    case HCI_EVENT_CONNECTION_COMPLETE:
        if(size<13) break;
        printf("ACL_COMPLETE status=0x%02x\n",p[2]); break;
    case HCI_EVENT_SYNCHRONOUS_CONNECTION_COMPLETE:
        if(size<19) break;
        printf("SCO_COMPLETE status=0x%02x rx_length=%u tx_length=%u interval=%u air_mode=%u\n",p[2],
            hci_event_synchronous_connection_complete_get_rx_packet_length(p),
            hci_event_synchronous_connection_complete_get_tx_packet_length(p),
            hci_event_synchronous_connection_complete_get_transmission_interval(p),
            hci_event_synchronous_connection_complete_get_air_mode(p)); break;
    case HCI_EVENT_SCO_CAN_SEND_NOW: send_sco(); break;
    default: break;
    }
}
int phone_init(const char *address,const phone_callbacks *callbacks, int pairing, int wideband) {
    if(!sscanf_bd_addr(address,phone)) return -1;
    client=*callbacks; allow_pairing=pairing;
    l2cap_init(); rfcomm_init(); sdp_init(); hfp_hf_init(1);
    uint16_t features=(1<<HFP_HFSF_CLI_PRESENTATION_CAPABILITY)|(1<<HFP_HFSF_REMOTE_VOLUME_CONTROL)|
                      (1<<HFP_HFSF_ENHANCED_CALL_STATUS)|(1<<HFP_HFSF_ESCO_S4);
    if(wideband) features|=1<<HFP_HFSF_CODEC_NEGOTIATION;
    uint8_t codecs[]={HFP_CODEC_CVSD,HFP_CODEC_MSBC};
    unsigned count=wideband?2:1;
    hfp_hf_init_supported_features(features); hfp_hf_init_codecs(count,codecs);
    hfp_hf_register_packet_handler(hf_event);
    hfp_hf_create_sdp_record_with_codecs(sdp_record,sdp_create_service_record_handle(),1,"HFPilot",features,count,codecs);
    sdp_register_service(sdp_record);
    hci_register_sco_packet_handler(phone_hci_event);
    gap_set_local_name("HFPilot"); gap_set_class_of_device(0x200408);
    gap_set_bondable_mode(pairing); gap_ssp_set_enable(1);
    gap_ssp_set_io_capability(SSP_IO_CAPABILITY_NO_INPUT_NO_OUTPUT); gap_ssp_set_auto_accept(0);
    gap_set_allow_role_switch(1);
    gap_register_classic_connection_filter(accept_connection);
    // Service registration enables page scan; use explicit outgoing connect only.
    gap_connectable_control(0); gap_discoverable_control(0);
    return 0;
}
int phone_command(const char *command,unsigned generation) {
    if(!strcmp(command,"connect") && acl==HCI_CON_HANDLE_INVALID)
        return hfp_hf_establish_service_level_connection(phone);
    if(generation!=state.generation || acl==HCI_CON_HANDLE_INVALID) return -1;
    if(!strcmp(command,"caller-query") && state.active==0 && state.setup==1 && state.held==0) {
        caller_query_generation=state.generation;
        return hfp_hf_query_current_call_status(acl);
    }
    if(!strcmp(command,"answer") && !answered_once && state.active==0 && state.setup==1 && state.held==0) {
        int result=hfp_hf_answer_incoming_call(acl);
        if(!result) { state.accepted=1; answered_once=1; changed(); }
        return result;
    }
    if(!strcmp(command,"hangup") && phone_can_hangup(&state)) return hfp_hf_terminate_call(acl);
    if(!state.accepted || state.active!=1 || state.setup!=0 || state.held!=0) return -1;
    if(!strcmp(command,"sco")) return hfp_hf_establish_audio_connection(acl);
    return -1;
}
int phone_dial(const char *number,unsigned generation) {
    if(answered_once || acl==HCI_CON_HANDLE_INVALID || !phone_number_valid(number) ||
       phone_reserve_outgoing(&state,generation)) return -1;
    // BTstack copies the number synchronously. Never retry an uncertain request.
    answered_once=1;
    char target[17]; strcpy(target,number);
    int result=hfp_hf_dial_number(acl,target);
    if(result) state.accepted=state.outgoing=state.dial_pending=0;
    changed();
    return result;
}
void phone_stop(void) {
    if(phone_can_hangup(&state)) {
        puts("CLEANUP_HANGUP"); hfp_hf_terminate_call(acl);
    }
}
