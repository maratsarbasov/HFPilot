// Native macOS Skywalk transport for the built-in controller. No USB or kext.
// API/ABI reference: apple-oss-distributions/xnu, bsd/skywalk/channel/os_channel.h.
// The system Bluetooth switch MUST be off. Busy channels cause a clean refusal.
#include <CoreFoundation/CoreFoundation.h>
#include <IOKit/IOKitLib.h>
#include <dlfcn.h>
#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <uuid/uuid.h>
#include "hci_transport.h"
#include "btstack_run_loop.h"
#include "btstack_util.h"

typedef void *channel_t;
typedef void *ring_t;
typedef const void *slot_t;
typedef struct {
    uint16_t flags, len;
    uint32_t idx;
    uint64_t ext_ptr, buf_ptr, mdata_ptr;
    uint32_t reserved[8];
} slot_prop_t;
_Static_assert(sizeof(slot_prop_t) == 64, "Skywalk ABI requires 64-byte properties");

static channel_t (*channel_create)(const unsigned char *, uint32_t);
static void (*channel_destroy)(channel_t);
static uint32_t (*channel_ring_id)(channel_t, int);
static ring_t (*channel_tx_ring)(channel_t, uint32_t);
static ring_t (*channel_rx_ring)(channel_t, uint32_t);
static int (*channel_sync)(channel_t, int);
static slot_t (*channel_get_next_slot)(ring_t, slot_t, slot_prop_t *);
static int (*channel_advance_slot)(ring_t, slot_t);
static void (*channel_set_slot_properties)(ring_t, slot_t, const slot_prop_t *);
static uint32_t (*channel_available_slot_count)(ring_t);
static uint64_t (*channel_slot_get_packet)(ring_t,slot_t);
static void *(*packet_get_next_buflet)(uint64_t,void *);
static void *(*buflet_get_object_address)(void *);
static uint32_t (*buflet_get_data_limit)(void *);
static int (*buflet_set_data_length)(void *,uint32_t);
static void (*packet_set_flow_uuid)(uint64_t,const unsigned char *);
static void (*packet_get_flow_uuid)(uint64_t,unsigned char (*)[16]);
static int (*packet_finalize)(uint64_t);
static struct { const char *name; uint8_t type; channel_t channel; ring_t rx, tx; uint64_t received, sent; } paths[] = {
    { .name="hci", .type=HCI_EVENT_PACKET },
    { .name="acl", .type=HCI_ACL_DATA_PACKET },
    { .name="sco", .type=HCI_SCO_DATA_PACKET }
};
static void (*packet_handler)(uint8_t, uint8_t *, uint16_t);
static btstack_timer_source_t poll_timer;
static int opened, pending_sent;
static uint8_t rx_storage[HCI_INCOMING_PRE_BUFFER_SIZE + 4096];
static int sync_path(unsigned path,int mode) {
    int result=channel_sync(paths[path].channel,mode);
    return result;
}
static void complete_sends(void) {
    // A copied Skywalk slot no longer references BTstack's shared TX buffer.
    // Deliver completion after returning from the stack callback, before the
    // next received SCO frame; waiting another timer tick loses paced credits.
    for(unsigned n=0;opened && pending_sent && n<8;n++) {
        pending_sent=0;
        uint8_t event[]={HCI_EVENT_TRANSPORT_PACKET_SENT,0};
        packet_handler(HCI_EVENT_PACKET,event,sizeof(event));
    }
}

static int close_transport(void) {
    opened = 0; pending_sent = 0;
    btstack_run_loop_remove_timer(&poll_timer);
    for (unsigned i=0; i<3; i++) {
        if (paths[i].channel) {
            channel_destroy(paths[i].channel);
            printf("TRANSPORT_CLOSED %s rx=%llu tx=%llu\n", paths[i].name,
                (unsigned long long)paths[i].received, (unsigned long long)paths[i].sent);
        }
        paths[i].channel = NULL; paths[i].rx = paths[i].tx = NULL;
    }
    return 0;
}
static void poll_transport(btstack_timer_source_t *timer) {
    if (!opened) return;
    complete_sends();
    for (unsigned i=0; opened && i<3; i++) {
        if (sync_path(i, 1) != 0) continue;
        for (unsigned count=0; opened && count<32; count++) {
            slot_prop_t prop = {0};
            slot_t slot = channel_get_next_slot(paths[i].rx, NULL, &prop);
            if (!slot) break;
            unsigned len = prop.len;
            if(i==2 && paths[i].received==0) {
                unsigned char flow[16]={0};
                packet_get_flow_uuid(channel_slot_get_packet(paths[i].rx,slot),&flow);
                printf("SCO_RX_METADATA length=%u flags=%u flow=",len,prop.flags);
                for(unsigned j=0;j<16;j++)printf("%02x",flow[j]);
                putchar('\n');
            }
            uint8_t *packet = rx_storage + HCI_INCOMING_PRE_BUFFER_SIZE;
            if (len && len <= 4096 && prop.buf_ptr) memcpy(packet, (void *)(uintptr_t)prop.buf_ptr, len);
            channel_advance_slot(paths[i].rx, slot);
            if (!len || len > 4096 || !prop.buf_ptr) continue;
            paths[i].received++;
            packet_handler(paths[i].type, packet, len);
            complete_sends();
        }
        if (opened) sync_path(i, 1);
    }
    if (opened) { btstack_run_loop_set_timer(timer, 2); btstack_run_loop_add_timer(timer); }
}
static int load_api(void) {
#define LOAD(name) do { *(void **)(&name) = dlsym(RTLD_DEFAULT, "os_" #name); if (!name) return -1; } while(0)
    LOAD(channel_create); LOAD(channel_destroy); LOAD(channel_ring_id);
    LOAD(channel_tx_ring); LOAD(channel_rx_ring); LOAD(channel_sync);
    LOAD(channel_get_next_slot); LOAD(channel_advance_slot);
    LOAD(channel_set_slot_properties); LOAD(channel_available_slot_count);
    LOAD(channel_slot_get_packet); LOAD(packet_get_next_buflet);
    LOAD(buflet_get_object_address); LOAD(buflet_get_data_limit);
    LOAD(buflet_set_data_length); LOAD(packet_set_flow_uuid); LOAD(packet_finalize);
    LOAD(packet_get_flow_uuid);
#undef LOAD
    return 0;
}
static int open_transport(void) {
    if (load_api()) { fprintf(stderr,"Skywalk API missing\n"); return -1; }
    io_iterator_t iter = 0;
    if (IOServiceGetMatchingServices(kIOMainPortDefault, IOServiceMatching("AppleConvergedIPCRTIInterface"), &iter)) return -1;
    io_service_t service;
    while ((service=IOIteratorNext(iter))) {
        CFStringRef protocol=IORegistryEntryCreateCFProperty(service, CFSTR("ACIPCInterfaceProtocol"), kCFAllocatorDefault, 0);
        char protocolName[32]={0};
        if (protocol && CFGetTypeID(protocol)==CFStringGetTypeID()) CFStringGetCString(protocol,protocolName,sizeof(protocolName),kCFStringEncodingUTF8);
        if (protocol) CFRelease(protocol);
        for (unsigned i=0; i<3; i++) {
            if (strcmp(protocolName,paths[i].name) || paths[i].channel) continue;
            CFStringRef value=IORegistryEntrySearchCFProperty(service,kIOServicePlane,CFSTR("IOSkywalkNexusUUID"),kCFAllocatorDefault,kIORegistryIterateRecursively);
            char uuidText[40]={0}; uuid_t uuid;
            if (value && CFGetTypeID(value)==CFStringGetTypeID()) CFStringGetCString(value,uuidText,sizeof(uuidText),kCFStringEncodingUTF8);
            if(value) CFRelease(value);
            if (uuid_parse(uuidText,uuid)) continue;
            paths[i].channel=channel_create(uuid,0);
            if (!paths[i].channel) { printf("TRANSPORT_OPEN_FAILED %s errno=%d %s\n",paths[i].name,errno,strerror(errno)); continue; }
            paths[i].rx=channel_rx_ring(paths[i].channel,channel_ring_id(paths[i].channel,2));
            paths[i].tx=channel_tx_ring(paths[i].channel,channel_ring_id(paths[i].channel,0));
            printf("TRANSPORT_OPENED %s\n",paths[i].name);
        }
        IOObjectRelease(service);
    }
    IOObjectRelease(iter);
    for(unsigned i=0;i<3;i++) if(!paths[i].channel || !paths[i].rx || !paths[i].tx) { close_transport(); return -1; }
    opened=1;
    btstack_run_loop_set_timer_handler(&poll_timer,poll_transport);
    btstack_run_loop_set_timer(&poll_timer,2); btstack_run_loop_add_timer(&poll_timer);
    return 0;
}
static int path_index(uint8_t type) {
    if(type==HCI_COMMAND_DATA_PACKET) return 0;
    if(type==HCI_ACL_DATA_PACKET) return 1;
    if(type==HCI_SCO_DATA_PACKET) return 2;
    return -1;
}
static int can_send(uint8_t type) {
    int i=path_index(type);
    return opened && !pending_sent && i>=0 && channel_available_slot_count(paths[i].tx)>0;
}
static int send_packet(uint8_t type,uint8_t *data,int size) {
    int i=path_index(type);
    if(!opened || i<0 || size<=0) return -1;
    slot_prop_t prop={0};
    slot_t slot=channel_get_next_slot(paths[i].tx,NULL,&prop);
    if(!slot || size>prop.len || !prop.buf_ptr) { fprintf(stderr,"TRANSPORT_TX_UNAVAILABLE %s size=%d capacity=%u\n",paths[i].name,size,prop.len); return -1; }
    if(type==HCI_SCO_DATA_PACKET) {
        // bluetoothd 26.3 uses packet metadata in addition to slot lengths.
        // A raw slot write alone passed ACL loopback but lost all outgoing SCO.
        uint64_t packet=channel_slot_get_packet(paths[i].tx,slot);
        void *buflet=packet_get_next_buflet(packet,NULL);
        if(!buflet || size>(int)buflet_get_data_limit(buflet)) return -1;
        unsigned char flow[16]={0};
        uint16_t handle=little_endian_read_16(data,0)&0x0fff;
        if(paths[i].sent==0) printf("SCO_TX_METADATA length=%d handle=%u capacity=%u\n",size,handle,prop.len);
        little_endian_store_16(flow,0,handle);little_endian_store_16(flow,8,handle);
        packet_set_flow_uuid(packet,flow);
        if(buflet_set_data_length(buflet,size)||packet_finalize(packet)) return -1;
        memcpy(buflet_get_object_address(buflet),data,size);
    } else memcpy((void *)(uintptr_t)prop.buf_ptr,data,size);
    prop.len=size;
    channel_set_slot_properties(paths[i].tx,slot,&prop);
    if(channel_advance_slot(paths[i].tx,slot) || sync_path((unsigned)i,0)) return -1;
    paths[i].sent++; pending_sent=1;
    // Log command opcodes only. Never packet payloads, pairing keys or PINs.
    if(type==HCI_COMMAND_DATA_PACKET && size>=3) printf("HCI_TX opcode=0x%04x\n",little_endian_read_16(data,0));
    return 0;
}
static void init_transport(const void *config) { (void)config; }
static void register_handler(void (*handler)(uint8_t,uint8_t *,uint16_t)) { packet_handler=handler; }
static const hci_transport_t transport={
    .name="macOS Skywalk", .init=init_transport, .open=open_transport,
    .close=close_transport, .register_packet_handler=register_handler,
    .can_send_packet_now=can_send, .send_packet=send_packet
};
const hci_transport_t *skywalk_transport_instance(void) { return &transport; }
