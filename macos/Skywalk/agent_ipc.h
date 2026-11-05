#ifndef SKYWALK_AGENT_IPC_H
#define SKYWALK_AGENT_IPC_H
int agent_init(const char *address, int control_fd, int audio_fd, int wideband);
void agent_tick(void);
void agent_working(void);
void agent_stop(void);
void agent_close(void);
int agent_finished(void);
#endif
