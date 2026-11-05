// HFP mSBC: 120 PCM samples / 7.5 ms, 2-byte H2 + 57-byte SBC + padding.
// HCI packets need not coincide with codec frames (e.g. 24, 30 or 60 bytes).
#include <string.h>
#include "classic/btstack_sbc_bluedroid.h"
#include "sco_codec.h"

enum { MSBC_BYTES=60, PCM_SAMPLES=120, PCM_BYTES=240, MAX_HCI_BYTES=255 };
static unsigned selected, sequence;
static phone_callbacks client;
static btstack_sbc_encoder_bluedroid_t encoder_context;
static OI_CODEC_SBC_DECODER_CONTEXT decoder_context;
static OI_UINT32 decoder_data[(DECODER_DATA_SIZE+3)/4];
static btstack_sbc_plc_state_t plc;
static const btstack_sbc_encoder_t *encoder;
static uint8_t encoded[MSBC_BYTES], received[MSBC_BYTES], rx_errors[MSBC_BYTES];
static int16_t pcm[PCM_SAMPLES];
static size_t tx_offset, rx_size;
static int expected_sequence;
static unsigned discarded;
static uint8_t sent_pcm[6*PCM_BYTES];
static size_t sent_size;
static unsigned long long good_frames, concealed_frames, bad_packets;

static int reset_decoder(void) {
    return OI_CODEC_mSBC_DecoderReset(&decoder_context,decoder_data,sizeof(decoder_data))!=OI_STATUS_SUCCESS;
}
static int decode(const uint8_t *frame,int16_t *out) {
    // Bluedroid's bit reader prefetches beyond the last coded byte. Keep that
    // look-ahead inside owned zero-padded storage, including the PLC frame.
    uint8_t padded[64]={0}; memcpy(padded,frame,57);
    const OI_BYTE *input=padded;
    OI_UINT32 bytes=57, pcm_bytes=PCM_BYTES;
    OI_STATUS status=OI_CODEC_SBC_DecodeFrame(&decoder_context,&input,&bytes,out,&pcm_bytes);
    return status!=OI_STATUS_SUCCESS || bytes || pcm_bytes!=PCM_BYTES;
}
static void deliver_frame(const uint8_t *frame,int bad) {
    int16_t raw[PCM_SAMPLES]={0}, output[PCM_SAMPLES];
    if(!bad && decode(frame,raw)) { bad=1; reset_decoder(); }
    if(bad) {
        // Advance the synthesis filter with the standard zero-input-response
        // frame before PLC. Always deliver 120 samples, including CRC failures.
        if(decode(btstack_sbc_plc_zero_signal_frame(),raw)) {
            reset_decoder(); memset(raw,0,sizeof(raw));
        }
        btstack_sbc_plc_bad_frame(&plc,raw,output); concealed_frames++;
    } else {
        btstack_sbc_plc_good_frame(&plc,raw,output); good_frames++;
    }
    // Don't replace concealed audio with zeros in the PCM IPC layer.
    if(client.received) client.received((uint8_t *)output,sizeof(output),0);
}
static int h2_sequence(const uint8_t *p) {
    if(p[0]!=1 || p[2]!=0xad) return -1;
    switch(p[1]) { case 8:return 0; case 0x38:return 1; case 0xc8:return 2; case 0xf8:return 3; }
    return -1;
}
int sco_codec_start(unsigned codec,const phone_callbacks *callbacks) {
    if(codec!=1 && codec!=2) return -1;
    selected=codec; client=*callbacks; sequence=discarded=0; expected_sequence=-1;
    tx_offset=MSBC_BYTES; rx_size=sent_size=0; good_frames=concealed_frames=bad_packets=0;
    if(codec==1) return 0;
    memset(&encoder_context,0,sizeof(encoder_context));
    memset(&decoder_context,0,sizeof(decoder_context));
    btstack_sbc_plc_init(&plc);
    if(reset_decoder()) return -1;
    encoder=btstack_sbc_encoder_bluedroid_init_instance(&encoder_context);
    if(encoder->configure(&encoder_context,SBC_MODE_mSBC,16,8,
        SBC_ALLOCATION_METHOD_LOUDNESS,16000,26,SBC_CHANNEL_MODE_MONO)) return -1;
    // Bluedroid sets the encoded length only when encoding the first frame.
    return encoder->num_audio_frames(&encoder_context)==PCM_SAMPLES ? 0 : -1;
}
int sco_codec_prepare(uint8_t *out,size_t n) {
    if(!n || n>MAX_HCI_BYTES) return -1;
    sent_size=0;
    if(selected==1) {
        if(n%2) return -1;
        memset(out,0,n);
        if(client.playback) client.playback(out,n);
        memcpy(sent_pcm,out,n); sent_size=n;
        return 0;
    }
    if(selected!=2) return -1;
    while(n) {
        if(tx_offset==MSBC_BYTES) {
            static const uint8_t h2[]={0x08,0x38,0xc8,0xf8};
            memset(pcm,0,sizeof(pcm));
            if(client.playback) client.playback((uint8_t *)pcm,sizeof(pcm));
            encoded[0]=1; encoded[1]=h2[sequence++ & 3]; encoded[59]=0;
            if(encoder->encode_signed_16(&encoder_context,pcm,encoded+2)) return -1;
            if(encoder->sbc_buffer_length(&encoder_context)!=57) return -1;
            tx_offset=0;
        }
        size_t count=MSBC_BYTES-tx_offset; if(count>n) count=n;
        memcpy(out,encoded+tx_offset,count); out+=count; n-=count; tx_offset+=count;
        if(tx_offset==MSBC_BYTES) {
            memcpy(sent_pcm+sent_size,pcm,sizeof(pcm)); sent_size+=sizeof(pcm);
        }
    }
    return 0;
}
void sco_codec_sent(void) {
    // Record the PCM supplied to fully submitted codec frames, not encoded bytes
    // or data merely queued in Python. This isn't a far-end reception recording.
    if(sent_size && client.sent) client.sent(sent_pcm,sent_size);
    sent_size=0;
}
void sco_codec_receive(const uint8_t *data,size_t n,unsigned status) {
    bad_packets+=(status!=0);
    if(selected==1) {
        if(n%2==0 && client.received) client.received(data,n,status!=0);
        return;
    }
    if(selected!=2) return;
    while(n) {
        received[rx_size]=*data++; rx_errors[rx_size++]=(status!=0); n--;
        if(rx_size<3) continue;
        int seq=h2_sequence(received);
        if(seq<0) {
            memmove(received,received+1,--rx_size);
            memmove(rx_errors,rx_errors+1,rx_size);
            if(expected_sequence>=0 && ++discarded==MSBC_BYTES) {
                deliver_frame(NULL,1); discarded=0;
                expected_sequence=(expected_sequence+1)&3;
            }
            continue;
        }
        if(rx_size<MSBC_BYTES) continue;
        if(expected_sequence>=0) {
            // H2 also reveals complete frames lost before reaching this process.
            // Its two-bit sequence can identify up to three missing frames.
            for(int missing=(seq-expected_sequence)&3;missing;missing--) deliver_frame(NULL,1);
        }
        int bad=0; for(unsigned i=0;i<MSBC_BYTES;i++) bad|=rx_errors[i];
        deliver_frame(received+2,bad);
        expected_sequence=(seq+1)&3; discarded=0; rx_size=0;
    }
}
void sco_codec_stats(phone_state *state) {
    state->codec=selected; state->sample_rate=selected==2?16000:8000;
    state->hci_bad_packets=bad_packets;
    state->msbc_good_frames=selected==2?good_frames:0;
    state->msbc_concealed_frames=selected==2?concealed_frames:0;
}
