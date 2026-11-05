#ifndef SKYWALK_PHONE_H
#define SKYWALK_PHONE_H
#include <stdint.h>
#include <stddef.h>

typedef struct {
    int received, type, source; // source: 1=CLIP, 2=CLCC
    char number[128], name[256];
} phone_caller;

// Decode the length-prefixed BTstack CLIP event. No Bluetooth access or logging.
int phone_caller_parse_clip(phone_caller *, const uint8_t *, size_t);
int phone_caller_parse_clcc(phone_caller *, const uint8_t *, size_t);

typedef struct {
    int slc, sco, active, setup, held, accepted;
    int outgoing, dial_pending;
    unsigned codec, sample_rate;
    int audio_error;
    unsigned long long hci_bad_packets, msbc_good_frames, msbc_concealed_frames;
    unsigned generation;
    phone_caller caller;
    phone_caller pending_caller;
    int ring_seen;
} phone_state;

void phone_caller_ring(phone_state *);
void phone_caller_begin(phone_state *, int incoming);
void phone_caller_clear(phone_state *);
int phone_caller_receive(phone_state *, const uint8_t *, size_t);
int phone_number_valid(const char *);
int phone_reserve_outgoing(phone_state *, unsigned generation);
void phone_indicator(phone_state *, const char *name, int value);
int phone_can_hangup(const phone_state *);

typedef struct {
    void (*state)(const phone_state *);
    void (*received)(const uint8_t *, size_t, int bad);
    void (*playback)(uint8_t *, size_t);
    void (*sent)(const uint8_t *, size_t);
} phone_callbacks;

int phone_init(const char *address, const phone_callbacks *callbacks, int pairing, int wideband);
const phone_state *phone_get_state(void);
int phone_command(const char *command, unsigned generation);
int phone_dial(const char *number, unsigned generation);
void phone_stop(void);
void phone_hci_event(uint8_t type, uint16_t channel, uint8_t *packet, uint16_t size);
#endif
