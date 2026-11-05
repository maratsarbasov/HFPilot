// Private inherited Unix datagrams. No listening socket, files, or network port.
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>
#include "phone.h"
#include "agent_ipc.h"

#define MAX_FRAME_BYTES 640 /* 20 ms, s16le mono, 16 kHz */
static unsigned sample_rate=8000;
#define FRAME_BYTES (sample_rate / 25) /* 20 ms at the negotiated rate. */
#define QUEUE_FRAMES 16 /* 320 ms: 260 ms scheduler budget plus refill margin. */
#define QUEUE_BYTES (QUEUE_FRAMES * FRAME_BYTES)
static int control=-1, audio=-1, finished, streaming;
static pid_t parent;
static uint64_t last_command, last_state, last_tick, pause_at, rx_samples, tx_samples;
static int previous_sco, tx_started;
static uint32_t epoch, requested_frames, received_frames;
static unsigned long long rx_dropped, tx_dropped, bad_packets, tx_underrun, tx_startup, sco_gap, tick_gap;
static uint8_t pending[QUEUE_FRAMES*MAX_FRAME_BYTES];
static size_t pending_bytes, queue_high_water;
struct media_batch { uint8_t data[MAX_FRAME_BYTES]; size_t size; uint64_t offset; };
static struct media_batch rx_batch, tx_batch;
static void read_playback(void);

static uint64_t now_ms(void) {
    struct timespec ts; clock_gettime(CLOCK_MONOTONIC,&ts);
    return (uint64_t)ts.tv_sec*1000+(uint64_t)ts.tv_nsec/1000000;
}
static void emit(const char *json) {
    size_t n=strlen(json);
    if(control<0 || send(control,json,n,MSG_DONTWAIT)!=(ssize_t)n) finished=1;
}
static void put_u32(uint8_t *p,uint32_t n) {
    for(unsigned i=0;i<4;i++) p[i]=(uint8_t)(n>>(i*8));
}
static uint32_t get_u32(const uint8_t *p) {
    uint32_t n=0; for(unsigned i=0;i<4;i++) n|=(uint32_t)p[i]<<(i*8); return n;
}
static void put_u64(uint8_t *p,uint64_t n) {
    for(unsigned i=0;i<8;i++) p[i]=(uint8_t)(n>>(i*8));
}
static void flush_batch(struct media_batch *batch,int kind) {
    if(!batch->size) return;
    uint8_t packet[9+MAX_FRAME_BYTES];
    packet[0]=(uint8_t)kind; put_u64(packet+1,batch->offset);
    memcpy(packet+9,batch->data,batch->size);
    if(send(audio,packet,batch->size+9,MSG_DONTWAIT)!=(ssize_t)(batch->size+9)) rx_dropped++;
    batch->size=0;
}
static void report_audio(struct media_batch *batch,int kind,const uint8_t *data,size_t n,uint64_t offset) {
    if(n%2) { finished=1; return; }
    if(batch->size && batch->offset+batch->size/2!=offset) flush_batch(batch,kind);
    while(n) {
        if(!batch->size) batch->offset=offset;
        size_t count=FRAME_BYTES-batch->size; if(count>n) count=n;
        memcpy(batch->data+batch->size,data,count); batch->size+=count;
        data+=count; n-=count; offset+=count/2;
        if(batch->size==FRAME_BYTES) flush_batch(batch,kind);
    }
}
static void flush_media(void) { flush_batch(&rx_batch,1); flush_batch(&tx_batch,2); }
static void reset_playback(void) {
    pending_bytes=0; requested_frames=received_frames=0; tx_started=0; epoch++;
}
static void hex_text(char *out,const char *text,size_t capacity) {
    static const char hex[]="0123456789abcdef";
    size_t n=strnlen(text,capacity);
    for(size_t i=0;i<n;i++) {
        unsigned char byte=(unsigned char)text[i];
        out[2*i]=hex[byte>>4]; out[2*i+1]=hex[byte&15];
    }
    out[2*n]=0;
}
static void credit_playback(void) {
    if(!streaming || !phone_get_state()->sco) return;
    size_t reserved=pending_bytes+(size_t)(requested_frames-received_frames)*FRAME_BYTES;
    if(reserved>QUEUE_BYTES) { finished=1; return; }
    unsigned count=(unsigned)((QUEUE_BYTES-reserved)/FRAME_BYTES);
    if(!count) return;
    // Cumulative credits tolerate batching at the Python end. Unreceived credits
    // still reserve space; neither a slow consumer nor a burst can overfill it.
    uint8_t message[9]={3}; put_u32(message+1,epoch); put_u32(message+5,requested_frames+count);
    if(send(audio,message,sizeof(message),MSG_DONTWAIT)==sizeof(message)) requested_frames+=count;
}
static void state_changed(const phone_state *s) {
    if(s->audio_error || (streaming && s->sco && s->sample_rate!=sample_rate)) {
        // A recording has one sample clock. Fail closed on a mid-call codec
        // change, rather than replaying PCM at the wrong speed after reconnect.
        flush_media(); streaming=0; pending_bytes=0; finished=1;
        emit("{\"event\":\"audio-error\",\"message\":\"SCO codec failed or changed during the active stream\"}");
    }
    if(streaming && !s->sco && previous_sco) {
        flush_media(); pause_at=now_ms(); reset_playback();
    }
    if(streaming && s->sco && !previous_sco) {
        // A new SCO link has a new playback epoch. Never replay old queued speech.
        uint64_t gap=pause_at ? (now_ms()-pause_at)*sample_rate/1000 : 0;
        rx_samples+=gap; tx_samples+=gap; sco_gap+=gap; pause_at=0;
    }
    if(s->active!=1) { flush_media(); streaming=0; pending_bytes=0; }
    previous_sco=s->sco;
    // Hex preserves the phone's bytes without letting quotes, control bytes or
    // invalid UTF-8 break the control JSON. Python decodes only bounded fields.
    char number[2*sizeof(s->caller.number)+1], name[2*sizeof(s->caller.name)+1];
    hex_text(number,s->caller.number,sizeof(s->caller.number));
    hex_text(name,s->caller.name,sizeof(s->caller.name));
    char msg[2048];
    snprintf(msg,sizeof(msg),"{\"event\":\"state\",\"slc\":%d,\"sco\":%d,\"call\":%d,\"setup\":%d,\"held\":%d,\"accepted\":%d,\"outgoing\":%d,\"dial_pending\":%d,\"generation\":%u,\"streaming\":%d,\"rx_samples\":%llu,\"tx_samples\":%llu,\"rx_ipc_dropped\":%llu,\"tx_dropped_bytes\":%llu,\"bad_sco_packets\":%llu,\"tx_silence_bytes\":%llu,\"tx_startup_silence_bytes\":%llu,\"sco_gap_samples\":%llu,\"tx_queue_high_water_bytes\":%zu,\"helper_tick_max_gap_ms\":%llu,\"caller_received\":%d,\"caller_type\":%d,\"caller_source\":%d,\"caller_number_hex\":\"%s\",\"caller_name_hex\":\"%s\"}",
        s->slc,s->sco,s->active,s->setup,s->held,s->accepted,s->outgoing,s->dial_pending,s->generation,streaming,
        (unsigned long long)rx_samples,(unsigned long long)tx_samples,
        rx_dropped,tx_dropped,bad_packets,tx_underrun,tx_startup,sco_gap,queue_high_water,tick_gap,
        s->caller.received,s->caller.type,s->caller.source,number,name);
    size_t end=strlen(msg)-1;
    snprintf(msg+end,sizeof(msg)-end,",\"codec\":%u,\"rate\":%u,\"frame_bytes\":%u,\"hci_bad_packets\":%llu,\"msbc_good_frames\":%llu,\"msbc_concealed_frames\":%llu}",
        s->codec,s->sample_rate,s->sample_rate/25,s->hci_bad_packets,s->msbc_good_frames,s->msbc_concealed_frames);
    emit(msg); last_state=now_ms();
}
static void received(const uint8_t *data,size_t n,int bad) {
    if(!streaming) return;
    uint8_t silence[512]={0};
    if(n>sizeof(silence)) { finished=1; return; }
    bad_packets+=bad;
    report_audio(&rx_batch,1,bad?silence:data,n,rx_samples); rx_samples+=n/2;
}
static void playback(uint8_t *out,size_t n) {
    if(!streaming) return;
    // SCO callbacks can arrive in a batch before the next run-loop timer.
    // Drain already delivered PCM before declaring underrun, not 5 ms later.
    if(pending_bytes<2*FRAME_BYTES) read_playback();
    size_t count=n<pending_bytes?n:pending_bytes;
    memcpy(out,pending,count); memmove(pending,pending+count,pending_bytes-count);
    pending_bytes-=count;
    if(tx_started) tx_underrun+=n-count; else tx_startup+=n-count;
    credit_playback();
}
static void sent(const uint8_t *data,size_t n) {
    if(!streaming) return;
    report_audio(&tx_batch,2,data,n,tx_samples); tx_samples+=n/2;
}
static int prepare_fd(int fd) {
    int type=0; socklen_t len=sizeof(type);
    struct sockaddr address; socklen_t alen=sizeof(address);
    if(fd<3 || getsockopt(fd,SOL_SOCKET,SO_TYPE,&type,&len) || type!=SOCK_DGRAM ||
       getsockname(fd,&address,&alen) || address.sa_family!=AF_UNIX) return -1;
    int flags=fcntl(fd,F_GETFL);
    if(flags<0 || fcntl(fd,F_SETFL,flags|O_NONBLOCK)) return -1;
    return 0;
}
int agent_init(const char *address,int control_fd,int audio_fd,int wideband) {
    if(control_fd==audio_fd || prepare_fd(control_fd) || prepare_fd(audio_fd)) return -1;
    control=control_fd; audio=audio_fd; parent=getppid(); last_command=now_ms();
    phone_callbacks callbacks={.state=state_changed,.received=received,.playback=playback,.sent=sent};
    int result=phone_init(address,&callbacks,0,wideband);
    if(!result) {
        char ready[160];
        snprintf(ready,sizeof(ready),"{\"event\":\"ready\",\"protocol\":3,\"rates\":[8000,16000],\"playback_frames\":%d,\"dial\":true}",QUEUE_FRAMES);
        emit(ready);
    }
    return result;
}
static void command(const char *msg) {
    unsigned id, generation; char name[24], number[18], extra;
    int fields=sscanf(msg,"%u %23s %u %17s %c",&id,name,&generation,number,&extra);
    if(fields<3 || fields>4) { finished=1; return; }
    int argument=!strcmp(name,"dial") || !strcmp(name,"audio-start");
    if((argument && fields!=4) || (!argument && fields!=3)) {
        finished=1; return;
    }
    last_command=now_ms();
    const phone_state *s=phone_get_state();
    int status=-1;
    if(!strcmp(name,"dial")) status=phone_dial(number,generation);
    else if(!strcmp(name,"status")) { state_changed(s); status=0; }
    else if(!strcmp(name,"quit")) { finished=1; status=0; }
    else if(!strcmp(name,"audio-stop") && (generation==s->generation || !streaming)) {
        // The accepted call may already have ended and advanced generation.
        // An idempotent stop of an inactive stream cannot affect another call.
        flush_media(); streaming=0; pending_bytes=0; status=0; state_changed(s);
    } else if(!strcmp(name,"audio-start") && generation==s->generation &&
              s->accepted && s->active==1 && s->setup==0 && s->held==0 && s->sco && !streaming && !finished &&
              ((s->codec==1 && s->sample_rate==8000 && !strcmp(number,"8000")) ||
               (s->codec==2 && s->sample_rate==16000 && !strcmp(number,"16000")))) {
        sample_rate=s->sample_rate;
        uint8_t stale[4+MAX_FRAME_BYTES]; while(recv(audio,stale,sizeof(stale),MSG_DONTWAIT)>0) {}
        reset_playback(); rx_samples=tx_samples=0; pause_at=0;
        rx_dropped=tx_dropped=bad_packets=tx_underrun=tx_startup=sco_gap=tick_gap=0;
        queue_high_water=0; last_tick=now_ms(); streaming=1; status=0;
        state_changed(s);
    } else status=phone_command(name,generation);
    char reply[128]; snprintf(reply,sizeof(reply),"{\"event\":\"ack\",\"id\":%u,\"status\":%d}",id,status);
    emit(reply);
}
static void read_playback(void) {
    uint8_t data[4+MAX_FRAME_BYTES+1];
    for(unsigned i=0;i<64;i++) {
        ssize_t n=recv(audio,data,sizeof(data),MSG_DONTWAIT);
        if(n<0 && (errno==EAGAIN || errno==EWOULDBLOCK)) break;
        if(n<4 || n>(ssize_t)(4+MAX_FRAME_BYTES)) { finished=1; return; }
        if(!streaming || !phone_get_state()->sco || get_u32(data)!=epoch) continue;
        if(n!=4+FRAME_BYTES) { finished=1; return; }
        if(received_frames>=requested_frames || pending_bytes+FRAME_BYTES>QUEUE_BYTES) {
            tx_dropped+=FRAME_BYTES; finished=1; return; // Protocol violation, never silently discard speech.
        }
        memcpy(pending+pending_bytes,data+4,FRAME_BYTES); pending_bytes+=FRAME_BYTES; received_frames++;
        tx_started=1; if(pending_bytes>queue_high_water) queue_high_water=pending_bytes;
    }
}
void agent_tick(void) {
    if(finished) return;
    uint64_t now=now_ms();
    if(streaming && now-last_tick>tick_gap) tick_gap=now-last_tick;
    last_tick=now;
    if(getppid()!=parent || now-last_command>10000) { finished=1; return; }
    char msg[128];
    for(unsigned i=0;i<16;i++) {
        ssize_t n=recv(control,msg,sizeof(msg)-1,MSG_DONTWAIT);
        if(n<0 && (errno==EAGAIN || errno==EWOULDBLOCK)) break;
        if(n<=0 || n==(ssize_t)sizeof(msg)-1) { finished=1; return; }
        msg[n]=0; command(msg);
    }
    read_playback();
    credit_playback();
    if(now_ms()-last_state>=1000) state_changed(phone_get_state());
}
void agent_stop(void) { flush_media(); streaming=0; pending_bytes=0; phone_stop(); }
void agent_close(void) {
    flush_media();
    if(control>=0) { state_changed(phone_get_state()); emit("{\"event\":\"stopped\"}"); close(control); }
    if(audio>=0) close(audio);
    control=audio=-1;
}
int agent_finished(void) { return finished; }
void agent_working(void) { emit("{\"event\":\"working\"}"); }
