// Offline exercise of the production codec: no HCI controller or call.
#include <assert.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "sco_codec.h"

static FILE *output;
static unsigned sample, submitted;
static void playback(uint8_t *out,size_t n) {
    assert(n%2==0);
    for(size_t i=0;i<n;i+=2) {
        int16_t value=(int16_t)(10000*sin(2*3.141592653589793*5000*sample++/16000));
        memcpy(out+i,&value,2);
    }
}
static void received(const uint8_t *data,size_t n,int bad) {
    assert(!bad && n==240);
    assert(fwrite(data,1,n,output)==n);
}
static void sent(const uint8_t *data,size_t n) { (void)data; submitted+=(unsigned)n/2; }
int main(int argc,char **argv) {
    if(argc!=4) return 2;
    unsigned chunk=(unsigned)atoi(argv[1]), fault=(unsigned)atoi(argv[2]);
    if(chunk<1 || chunk>255 || fault>4) return 2;
    output=fopen(argv[3],"wb"); if(!output) return 2;
    phone_callbacks callbacks={.playback=playback,.received=received,.sent=sent};
    assert(sco_codec_start(2,&callbacks)==0);
    uint8_t wire[60*160];
    for(size_t offset=0;offset<sizeof(wire);) {
        size_t n=sizeof(wire)-offset; if(n>chunk) n=chunk;
        assert(sco_codec_prepare(wire+offset,n)==0);
        sco_codec_sent(); offset+=n;
    }
    const uint8_t h2[]={8,0x38,0xc8,0xf8};
    for(unsigned i=0;i<160;i++) {
        assert(wire[60*i]==1 && wire[60*i+1]==h2[i%4]);
        assert(wire[60*i+2]==0xad && wire[60*i+59]==0);
    }
    assert(submitted==160*120);
    if(fault==2) wire[60*50+5]^=0xff; // Corrupt CRC-protected subband header.
    for(size_t offset=0;offset<sizeof(wire);) {
        size_t n=sizeof(wire)-offset; if(n>chunk) n=chunk;
        unsigned bad=(fault==1 && offset<=60*50 && offset+n>60*50) || (fault==4 && offset==0);
        // An actually missing fragment tests byte resynchronisation.
        if(!(fault==3 && offset<=60*50 && offset+n>60*50))
            sco_codec_receive(wire+offset,n,bad);
        offset+=n;
    }
    phone_state state={0}; sco_codec_stats(&state);
    fclose(output);
    printf("{\"rate\":%u,\"good\":%llu,\"concealed\":%llu,\"bad_packets\":%llu}\n",
           state.sample_rate,state.msbc_good_frames,state.msbc_concealed_frames,state.hci_bad_packets);
    // A new SCO link starts a fresh H2 sequence and codec state.
    assert(sco_codec_start(2,&callbacks)==0);
    assert(sco_codec_prepare(wire,60)==0 && wire[1]==8);
    return 0;
}
