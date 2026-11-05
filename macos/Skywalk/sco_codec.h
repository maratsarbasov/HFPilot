#ifndef SKYWALK_SCO_CODEC_H
#define SKYWALK_SCO_CODEC_H
#include "phone.h"

// One SCO link. Callbacks always carry s16le PCM, never compressed HCI data.
// CVSD is converted by the controller; mSBC uses the software codec below.
int sco_codec_start(unsigned codec, const phone_callbacks *callbacks);
int sco_codec_prepare(uint8_t *payload, size_t size);
void sco_codec_sent(void); // Only after HCI accepted the prepared payload.
void sco_codec_receive(const uint8_t *payload, size_t size, unsigned status);
void sco_codec_stats(phone_state *state);
#endif
